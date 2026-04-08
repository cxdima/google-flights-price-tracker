"""
AWS persistence layer — DynamoDB price history and S3 utilities.
"""
import json
import logging
import time

import boto3
from boto3.dynamodb.conditions import Key

from config import AWS_REGION, DYNAMODB_TABLE, S3_BUCKET

log = logging.getLogger(__name__)

# DynamoDB TTL: keep records for 1 year
_TTL_SECS = 365 * 24 * 60 * 60


# ── Client factories (cached — boto3 clients are thread-safe) ─────────────────

_s3_client = None
_ddb_resource = None


def get_dynamodb():
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _ddb_resource


def get_s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


# ── Price history ──────────────────────────────────────────────────────────────

def get_last_price(table, flight_id: str) -> dict | None:
    """
    Query DynamoDB for the most recent price record of a flight.
    Returns the item dict or None if no history exists.
    """
    try:
        resp = table.query(
            KeyConditionExpression=Key("flight_id").eq(flight_id),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except Exception as exc:
        log.error("DynamoDB get_last_price failed: %s", exc)
        return None


def write_price(table, record: dict, meta: dict | None = None) -> None:
    """
    Persist a price record to DynamoDB.
    Uses the current epoch-millisecond timestamp as the range key so we never
    overwrite existing history — we only append.

    If `meta` is provided, flight details (airline, route, times, etc.) are
    stored alongside the price so each row is self-contained.
    """
    try:
        now_ms = int(time.time() * 1000)
        now_s  = int(time.time())
        item: dict = {
            "flight_id":    record["flight_id"],
            "ts":           now_ms,
            "price":        int(record["price"]),
            "prev_price":   int(record["prev_price"])    if record.get("prev_price")    is not None else None,
            "price_change": int(record["price_change"])  if record.get("price_change")  is not None else None,
            "search_url":   record.get("search_url"),
            "ttl":          now_s + _TTL_SECS,
        }
        if meta:
            for field in (
                "airline", "flight_numbers",
                "origin", "destination",
                "departure_date", "departure_time",
                "arrival_date",  "arrival_time",
                "duration_min",  "stops", "via",
            ):
                val = meta.get(field)
                if val is not None:
                    item[field] = val
            # search_url from meta as fallback
            if not item.get("search_url") and meta.get("search_url"):
                item["search_url"] = meta["search_url"]

        table.put_item(Item=item)
        log.info("Wrote price $%d for flight %s", record["price"], record["flight_id"][:16])
    except Exception as exc:
        log.error("DynamoDB write_price failed: %s", exc)


# ── Flights manifest (S3 JSON) ─────────────────────────────────────────────────
# A lightweight JSON file in S3 that always reflects every flight currently
# being tracked — used by the /flights Telegram command without a DynamoDB scan.

_MANIFEST_KEY = "flights/manifest.json"
_STATUS_KEY   = "status/last_run.json"


def update_flights_manifest(s3, manifest: dict) -> None:
    """Overwrite the flights manifest in S3."""
    if not S3_BUCKET:
        return
    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=_MANIFEST_KEY,
            Body=json.dumps(manifest, default=str).encode(),
            ContentType="application/json",
        )
        log.info("Flights manifest updated (%d entries)", len(manifest))
    except Exception as exc:
        log.error("update_flights_manifest failed: %s", exc)


def get_flights_manifest(s3) -> dict:
    """Read the flights manifest from S3. Returns {} if not found."""
    if not S3_BUCKET:
        return {}
    try:
        raw = s3.get_object(Bucket=S3_BUCKET, Key=_MANIFEST_KEY)["Body"].read()
        return json.loads(raw)
    except Exception:
        return {}


def save_last_summary(s3, summary: dict) -> None:
    """Persist the last run summary to S3 so /status survives cold starts."""
    if not S3_BUCKET:
        return
    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=_STATUS_KEY,
            Body=json.dumps(summary).encode(),
            ContentType="application/json",
        )
    except Exception as exc:
        log.error("save_last_summary failed: %s", exc)


def load_last_summary(s3) -> dict | None:
    """Load the last run summary from S3. Returns None if not available."""
    if not S3_BUCKET:
        return None
    try:
        raw = s3.get_object(Bucket=S3_BUCKET, Key=_STATUS_KEY)["Body"].read()
        return json.loads(raw)
    except Exception:
        return None
