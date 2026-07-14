"""
Price fetching and parsing.

The captured GetSolutionPrices POSTs are re-fired from plain Python with
the browser's cookies — Chrome can be shut down first, freeing ~800 MB
before any network/DB work happens.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request

from gfpt.models import PriceQuote

log = logging.getLogger(__name__)

# Sanity bounds for "this integer is a ticket price in USD"
MIN_PLAUSIBLE_PRICE = 29
MAX_PLAUSIBLE_PRICE = 50_000

# id_<32 lowercase hex chars>. A broad pattern would also match the long
# url-safe base64 protobuf tokens that appear throughout the response.
_FLIGHT_ID_RE = re.compile(r"id_[a-f0-9]{32}")


# ── Re-firing captured requests ────────────────────────────────────────────────

def extract_params(captured: dict) -> dict:
    """
    Parse query-string and POST body of a captured request into a flat dict.
    """
    parsed_url = urllib.parse.urlparse(captured["url"])
    qs = urllib.parse.parse_qs(parsed_url.query)
    post_qs = urllib.parse.parse_qs(captured.get("postData", ""))

    return {
        "url":          captured["url"],
        "base_url":     f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}",
        "f_sid":        qs.get("f.sid",        [""])[0],
        "bl":           qs.get("bl",           [""])[0],
        "hl":           qs.get("hl",           ["en-US"])[0],
        "gl":           qs.get("gl",           ["US"])[0],
        "soc_app":      qs.get("soc-app",      ["162"])[0],
        "soc_platform": qs.get("soc-platform", ["1"])[0],
        "soc_device":   qs.get("soc-device",   ["4"])[0],
        "_reqid":       qs.get("_reqid",       ["142865"])[0],
        "rt":           qs.get("rt",           ["c"])[0],
        "at":           post_qs.get("at",      [""])[0],
        "f_req":        post_qs.get("f.req",   ["[]"])[0],
    }


def fetch_prices(
    params: dict,
    headers: dict,
    attempts: int = 1,
    deadline: float | None = None,
) -> bytes:
    """
    Re-fire a single GetSolutionPrices POST with pre-built HTTP headers.
    Pure stdlib — no Selenium dependency, safe to call from threads.

    Google's bot defense intermittently tarpits these requests from AWS
    egress IPs (>20s stall) — measured per-attempt-random, so a retry on a
    fresh connection re-rolls the dice. Each attempt gets its own 20s read
    timeout; the last failure propagates. The first attempt always runs;
    retries stop once time.monotonic() passes `deadline` (a shared
    wall-clock budget when many requests run through one thread pool).
    """
    query = urllib.parse.urlencode({
        "f.sid":        params["f_sid"],
        "bl":           params["bl"],
        "hl":           params["hl"],
        "gl":           params["gl"],
        "soc-app":      params["soc_app"],
        "soc-platform": params["soc_platform"],
        "soc-device":   params["soc_device"],
        "_reqid":       params["_reqid"],
        "rt":           params["rt"],
    })
    url = f"{params['base_url']}?{query}"
    body = urllib.parse.urlencode({
        "f.req": params["f_req"],
        "at":    params["at"],
    }).encode()

    last_exc: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                if deadline is not None and time.monotonic() >= deadline:
                    log.warning("GetSolutionPrices attempt %d/%d failed (%s) — "
                                "retry budget exhausted", attempt, attempts, exc)
                    break
                log.warning("GetSolutionPrices attempt %d/%d failed (%s) — retrying",
                            attempt, attempts, exc)
    raise last_exc


# ── Response parsing ───────────────────────────────────────────────────────────

def parse_prices(body: bytes) -> list[PriceQuote]:
    """
    Extract price quotes from the wrb.fr response frames, deduplicated by
    flight_id (keeping the lowest price), sorted ascending by price.
    """
    text = body.decode("utf-8", errors="replace")

    # Strip the XSRF safety prefix
    if text.startswith(")]}'"):
        text = text[4:].lstrip("\n")

    records: dict[str, PriceQuote] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            outer = json.loads(line)
        except ValueError:
            continue

        for frame in (outer if isinstance(outer, list) else [outer]):
            if not isinstance(frame, list) or len(frame) < 3:
                continue
            if frame[0] != "wrb.fr":
                continue
            try:
                inner = json.loads(frame[2])
            except (TypeError, ValueError):
                continue
            _collect_quotes(inner, records)

    return sorted(records.values(), key=lambda q: q.price)


def _collect_quotes(inner, records: dict[str, PriceQuote]) -> None:
    """Walk a decoded wrb.fr frame and extract (flight_id, price) pairs.

    Flight records may nest at arbitrary depth, so every list item is
    visited — not just the first flight found in each top-level item.
    """
    if not isinstance(inner, list):
        return

    for item in inner:
        if not isinstance(item, list):
            continue

        flight_id = _find_flight_id(item)
        price = _find_price(item)
        if flight_id and price is not None:
            existing = records.get(flight_id)
            if existing is None or price < existing.price:
                records[flight_id] = PriceQuote(
                    flight_id=flight_id,
                    price=price,
                    search_url=_find_search_url(item),
                )

        # Always recurse — the same item may contain further nested flights.
        _collect_quotes(item, records)


def _find_flight_id(obj) -> str | None:
    """Return the first string matching the saved-flight ID pattern."""
    if isinstance(obj, str) and _FLIGHT_ID_RE.fullmatch(obj):
        return obj
    if isinstance(obj, list):
        for item in obj:
            result = _find_flight_id(item)
            if result:
                return result
    return None


def _find_price(obj) -> int | None:
    """Return the first number in the plausible ticket-price range."""
    if isinstance(obj, bool):
        return None
    if isinstance(obj, (int, float)) and MIN_PLAUSIBLE_PRICE <= obj <= MAX_PLAUSIBLE_PRICE:
        return int(obj)
    if isinstance(obj, list):
        for item in obj:
            result = _find_price(item)
            if result is not None:
                return result
    return None


def _find_search_url(obj) -> str | None:
    """Return the first Google Flights URL found in the structure."""
    if isinstance(obj, str) and "google.com/travel/flights" in obj:
        return obj
    if isinstance(obj, list):
        for item in obj:
            result = _find_search_url(item)
            if result:
                return result
    return None
