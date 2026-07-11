"""
DynamoDB price history.

One item per new-low event, keyed by (flight_id, epoch-ms timestamp) so
history is append-only. Items expire after a year via the table's TTL.
"""
from __future__ import annotations

import logging
import time

import boto3
from boto3.dynamodb.conditions import Key

from gfpt.models import PriceQuote

log = logging.getLogger(__name__)

_TTL_SECS = 365 * 24 * 60 * 60

# Metadata fields copied onto each price row so history is self-contained
_META_FIELDS = (
    "airline", "flight_numbers", "origin", "destination",
    "departure_date", "departure_time", "arrival_date", "arrival_time",
    "duration_min", "stops", "via",
)


class PriceHistory:
    def __init__(self, table_name: str, dynamodb=None, region: str | None = None):
        resource = dynamodb or boto3.resource("dynamodb", region_name=region)
        self._table = resource.Table(table_name)

    def last_price(self, flight_id: str) -> int | None:
        """
        Return the most recently recorded price for a flight, or None if the
        flight has no history.

        Raises LookupError on a failed read: "couldn't read" must never look
        like "never seen" — that would re-announce an existing flight and
        reset its low-price watermark to whatever today's price is.
        """
        try:
            resp = self._table.query(
                KeyConditionExpression=Key("flight_id").eq(flight_id),
                ScanIndexForward=False,
                Limit=1,
            )
        except Exception as exc:
            log.error("DynamoDB last_price failed for %s: %s", flight_id[:16], exc)
            raise LookupError(f"price history read failed: {exc}") from exc
        items = resp.get("Items", [])
        return int(items[0]["price"]) if items else None

    def record(self, quote: PriceQuote, meta: dict | None = None,
               prev_price: int | None = None) -> bool:
        """Append a price record. Never raises — a lost write costs one
        history row, not the run."""
        try:
            now_s = int(time.time())
            item: dict = {
                "flight_id": quote.flight_id,
                "ts": now_s * 1000,
                "price": quote.price,
                "prev_price": prev_price,
                "search_url": quote.search_url,
                "ttl": now_s + _TTL_SECS,
            }
            for field_name in _META_FIELDS:
                value = (meta or {}).get(field_name)
                if value is not None:
                    item[field_name] = value
            if not item.get("search_url") and (meta or {}).get("search_url"):
                item["search_url"] = meta["search_url"]

            self._table.put_item(Item=item)
            log.info("Recorded $%d for %s", quote.price, quote.flight_id[:16])
            return True
        except Exception as exc:
            log.error("DynamoDB record failed for %s: %s", quote.flight_id[:16], exc)
            return False
