"""
S3-backed JSON state.

Everything the tracker remembers between runs lives here as small JSON
objects. Keys are kept identical to the previous version so a redeploy
picks up existing state with no migration:

  sessions/latest.json   Google session cookies (+ saved_at timestamp)
  flights/manifest.json  every tracked flight: metadata, price, missing_runs
  status/last_run.json   last RunSummary, for /status
  state/users.json       per-user preferences (muted, ...)
  state/health.json      consecutive-failure counter
  screenshots/*.png      crash screenshots for post-mortems (3-day S3 expiry)

Reads degrade to defaults on any error (a missing key is normal on first
run); writes log an error but never crash a tracker run.
"""
from __future__ import annotations

import json
import logging
import time

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

# S3 error codes that just mean "state not written yet" — normal on first
# run. The role has s3:ListBucket, so a missing key returns a genuine
# NoSuchKey; AccessDenied therefore indicates real IAM breakage (it would
# make every read look like a clean state reset), and is logged as an error.
_MISSING_KEY_CODES = {"NoSuchKey", "NoSuchBucket", "404"}
_AMBIGUOUS_CODES = {"AccessDenied", "403"}

_SESSION_KEY = "sessions/latest.json"
_MANIFEST_KEY = "flights/manifest.json"
_SUMMARY_KEY = "status/last_run.json"
_USER_PREFS_KEY = "state/users.json"
_HEALTH_KEY = "state/health.json"


class StateStore:
    """Thin, typed wrapper around the project's S3 bucket."""

    def __init__(self, bucket: str, s3_client=None, region: str | None = None):
        self.bucket = bucket
        self._s3 = s3_client or boto3.client("s3", region_name=region)

    # ── Generic JSON helpers ───────────────────────────────────────────────────

    def _get_json(self, key: str, default):
        if not self.bucket:
            return default
        try:
            raw = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            return json.loads(raw)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _AMBIGUOUS_CODES:
                log.error(
                    "S3 read of %s returned %s — the role has s3:ListBucket, "
                    "so this is NOT a missing key: IAM/bucket-policy breakage "
                    "likely, state may appear reset", key, code,
                )
            elif code not in _MISSING_KEY_CODES:
                log.error("S3 read failed for %s: %s", key, exc)
            return default
        except Exception as exc:
            log.error("S3 read failed for %s: %s", key, exc)
            return default

    def _put_json(self, key: str, obj) -> bool:
        if not self.bucket:
            return False
        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=json.dumps(obj, default=str).encode(),
                ContentType="application/json",
            )
            return True
        except Exception as exc:
            log.error("S3 write failed for %s: %s", key, exc)
            return False

    # ── Google session cookies ─────────────────────────────────────────────────

    def load_session(self) -> dict | None:
        return self._get_json(_SESSION_KEY, None)

    def save_session(self, payload: dict) -> bool:
        return self._put_json(_SESSION_KEY, payload)

    # ── Flights manifest ───────────────────────────────────────────────────────
    # Shape: {flight_id: {<metadata fields>, "price": int, "search_url": str,
    #                     "missing_runs": int}, "__updated__": "<timestamp>"}

    def load_manifest(self) -> dict:
        manifest = self._get_json(_MANIFEST_KEY, {})
        return manifest if isinstance(manifest, dict) else {}

    def save_manifest(self, manifest: dict) -> bool:
        return self._put_json(_MANIFEST_KEY, manifest)

    # ── Last run summary ───────────────────────────────────────────────────────

    def load_summary(self) -> dict | None:
        return self._get_json(_SUMMARY_KEY, None)

    def save_summary(self, summary: dict) -> bool:
        return self._put_json(_SUMMARY_KEY, summary)

    # ── Per-user preferences ───────────────────────────────────────────────────
    # Shape: {chat_id: {"muted": bool, "threshold": int, "rise_alerts": bool,
    #                   "muted_routes": ["ORD-LAX", ...]}}

    def load_user_prefs(self) -> dict:
        prefs = self._get_json(_USER_PREFS_KEY, {})
        return prefs if isinstance(prefs, dict) else {}

    def save_user_prefs(self, prefs: dict) -> bool:
        return self._put_json(_USER_PREFS_KEY, prefs)

    # ── Health (failure streak + notification bookkeeping) ────────────────────
    # Shape: {"consecutive_failures": int, "failure_notified": bool,
    #         "login_failures": int}

    def load_health(self) -> dict:
        health = self._get_json(_HEALTH_KEY, {})
        return health if isinstance(health, dict) else {}

    def save_health(self, health: dict) -> bool:
        return self._put_json(_HEALTH_KEY, health)

    def load_failure_streak(self) -> int:
        try:
            return int(self.load_health().get("consecutive_failures", 0))
        except (TypeError, ValueError):
            return 0

    # ── Crash screenshots ──────────────────────────────────────────────────────

    def save_screenshot(self, label: str, png: bytes) -> str | None:
        if not self.bucket:
            return None
        key = f"screenshots/{label}-{int(time.time())}.png"
        try:
            self._s3.put_object(
                Bucket=self.bucket, Key=key, Body=png, ContentType="image/png"
            )
            return key
        except Exception as exc:
            log.error("Screenshot upload failed: %s", exc)
            return None
