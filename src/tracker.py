"""
Flight data extraction and price parsing.

Flow:
  1. intercept_api_call  — enable CDP, navigate, wait for GetSolutionPrices POST
  2. extract_flight_metadata — parse AF_initDataCallback blocks from page HTML
  3. extract_params       — pull f.sid / bl / at / f.req from the captured request
  4. fetch_prices         — re-fire the POST from Python (using browser cookies)
  5. parse_prices         — extract price records from the wrb.fr response frames
"""
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from config import FLIGHTS_SAVES_URL, HYDRATE_SECS

if TYPE_CHECKING:
    from selenium import webdriver

log = logging.getLogger(__name__)

_PRICES_ENDPOINT = "travel.frontend.flights.FlightsFrontendService/GetSolutionPrices"


# ── Network interception ───────────────────────────────────────────────────────

def intercept_api_call(
    driver: "webdriver.Chrome",
    skip_nav: bool = False,
) -> tuple[list[dict], str]:
    """
    Poll CDP performance logs until all GetSolutionPrices POSTs are captured.

    skip_nav=True  — caller already navigated to the saves page and CDP was
                     enabled before that navigation; skip the redundant reload.
    skip_nav=False — navigate now (first run or after a full login).

    Returns (list_of_unique_requests, page_html).
    Raises RuntimeError if no call is observed after all attempts.
    """
    driver.execute_cdp_cmd("Network.enable", {})

    if skip_nav:
        log.info("Skipping re-navigation — already on saves page (warm path)")
    else:
        log.info("Navigating to saves page — waiting up to %ds for API call", HYDRATE_SECS)
        driver.get(FLIGHTS_SAVES_URL)

    captured = _poll_for_prices(driver)

    # Warm-path fallback: if skip_nav captured nothing, the CDP logs were
    # likely consumed during session restore.  Force a page reload.
    if not captured and skip_nav:
        log.warning("Warm path captured nothing — reloading page")
        try:
            driver.get_log("performance")
        except Exception:
            pass
        driver.get(FLIGHTS_SAVES_URL)
        captured = _poll_for_prices(driver)

    if not captured:
        raise RuntimeError(
            f"GetSolutionPrices was not observed within {HYDRATE_SECS}s"
        )

    log.info("Captured %d unique GetSolutionPrices request(s)", len(captured))
    return captured, driver.page_source


def _poll_for_prices(driver: "webdriver.Chrome") -> list[dict]:
    """
    Poll CDP performance logs for GetSolutionPrices POSTs, scrolling the
    page to trigger lazy-loaded flight cards.

    Returns a list of unique captured requests (may be empty).
    """
    captured: list[dict] = []
    seen_post_data: set[str] = set()
    last_found_at: float | None = None
    # Exit after this many seconds of silence (no new GetSolutionPrices
    # requests).  5 s gives Google Flights enough time to fire a new batch
    # after each scroll step without us exiting too early.
    _SILENCE_SECS = 2.5
    deadline = time.monotonic() + HYDRATE_SECS

    # Scroll positions — Google Flights only fires GetSolutionPrices for rows
    # visible in the viewport.  Large values are harmless on shorter pages.
    _SCROLL_POS    = [600, 1400, 2500, 4000, 6000, 9000, 14000, 20000]
    _scroll_idx    = 0
    _last_scroll_t = time.monotonic()
    _SCROLL_EVERY  = 0.5   # seconds between scroll steps

    while time.monotonic() < deadline:
        found_new = False
        for entry in driver.get_log("performance"):
            try:
                msg = json.loads(entry["message"])["message"]
            except Exception:
                continue

            if msg.get("method") != "Network.requestWillBeSent":
                continue

            req = msg.get("params", {}).get("request", {})
            if _PRICES_ENDPOINT not in req.get("url", ""):
                continue

            # Deduplicate: the SPA may re-send the same request on retry.
            key = req.get("postData", "")
            if key in seen_post_data:
                continue
            seen_post_data.add(key)
            captured.append(req)
            found_new = True
            log.info("Captured GetSolutionPrices request #%d", len(captured))

        if found_new:
            last_found_at = time.monotonic()

        # Scroll down periodically to trigger lazy-loaded flight rows.
        if _scroll_idx < len(_SCROLL_POS):
            if time.monotonic() - _last_scroll_t >= _SCROLL_EVERY:
                pos = _SCROLL_POS[_scroll_idx]
                try:
                    driver.execute_script(f"window.scrollTo(0, {pos});")
                except Exception:
                    pass
                log.debug("Scrolled to y=%d (%d/%d)", pos, _scroll_idx + 1, len(_SCROLL_POS))
                _scroll_idx += 1
                _last_scroll_t = time.monotonic()

        # Exit only after silence — ensures all progressive batches are caught.
        if captured and last_found_at and (time.monotonic() - last_found_at) >= _SILENCE_SECS:
            break

        time.sleep(0.15)

    return captured


# ── Flight metadata ────────────────────────────────────────────────────────────

def extract_flight_metadata(page_html: str) -> dict:
    """
    Parse AF_initDataCallback script blocks from the saves-page HTML.

    The page stores saved-flight data as a nested array (not a dict with named
    keys), so we parse the known positional structure directly instead of
    walking for field names.

    Returns a dict keyed by flight_id (id_<32 hex chars>) with keys:
      airline, flight_numbers, origin, destination, departure_date,
      departure_time, arrival_date, arrival_time, duration_min,
      stops, via (list of stopover airports), search_url.
    """
    metadata: dict = {}

    blocks = re.findall(
        r"AF_initDataCallback\((\{.*?\})\);",
        page_html,
        re.DOTALL,
    )

    for block in blocks:
        # AF_initDataCallback uses JavaScript object-literal syntax
        # (unquoted keys, single-quoted strings) — NOT valid JSON.
        # Only the 'data:' value is a proper JSON array, so we extract
        # that field directly using a balanced-bracket search.
        payload = _extract_js_data_array(block)
        if not isinstance(payload, list):
            continue

        # Don't assume a fixed path — different blocks nest the
        # [flight_id, details] entries at different depths.  Recursively
        # search the entire data array for any list whose first element
        # matches the flight-ID pattern.
        _find_flight_entries(payload, metadata)

    log.info("Metadata extracted for %d flight(s)", len(metadata))
    return metadata


def _extract_js_data_array(block: str) -> list | None:
    """
    Pull the value of the 'data:' key from a JavaScript object-literal string
    and parse it as JSON.

    AF_initDataCallback({key: 'ds:1', hash: '5', data: [...], sideChannel: {}})
                                                          ^^^^ this part

    The outer JS object uses unquoted keys and single-quoted strings (invalid
    JSON), but the 'data' value itself is a standard JSON array we can parse
    directly.  We locate the opening '[' via regex and then walk the string to
    find the matching ']'.
    """
    m = re.search(r'"?data"?\s*:\s*(\[)', block)
    if not m:
        return None
    start = m.start(1)
    depth = 0
    for i, ch in enumerate(block[start:]):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(block[start : start + i + 1])
                except Exception:
                    return None
    return None


def _find_flight_entries(obj: list, results: dict) -> None:
    """
    Recursively search a nested list structure for [flight_id, details]
    entries and populate `results` with parsed metadata.

    Different AF_initDataCallback blocks place the flight entries at varying
    depths, so we can't rely on a fixed path like payload[1][N][0].  Instead
    we walk every sub-list and treat any list whose first element matches
    id_<32hex> as a flight entry.
    """
    if not isinstance(obj, list):
        return

    # Check if *this* list is a flight entry: [id_xxx, details_array, ...]
    if (
        len(obj) >= 2
        and isinstance(obj[0], str)
        and re.fullmatch(r"id_[a-f0-9]{32}", obj[0])
        and isinstance(obj[1], list)
    ):
        meta = _parse_flight_details(obj[1])
        if meta:
            results[obj[0]] = meta
            log.debug("Metadata for %s: %s", obj[0][:16], meta)
        # Don't recurse further into this matched entry — the details
        # array has numbers/strings that could confuse the search.
        return

    for item in obj:
        if isinstance(item, list):
            _find_flight_entries(item, results)


def _parse_flight_details(details: list) -> dict:
    """
    Parse one flight's positional details array from AF_initDataCallback.

    Confirmed positions (from page inspection):
      [1]  relative search URL  (/travel/flights?tfs=...)
      [4]  [ [segment_row, ...], ["Airline Name"] ]

    segment_row positions:
      [1]  ["Airline Name"]
      [2]  [["IATA_code", "flight_num"], ...]  — one per leg
      [3]  origin IATA
      [4]  departure date  [y, m, d]
      [5]  departure time  [h, m]
      [6]  destination IATA
      [7]  arrival date    [y, m, d]
      [8]  arrival time    [h, m]
      [9]  duration in minutes
      [11] stopover airports list, or null if direct
    """
    meta: dict = {}

    # Search URL
    if len(details) > 1 and isinstance(details[1], str) and "/travel/flights" in details[1]:
        url = details[1].split("&authuser")[0]
        meta["search_url"] = f"https://www.google.com{url}" if url.startswith("/") else url

    if len(details) <= 4 or not isinstance(details[4], list) or not details[4]:
        return meta

    outer = details[4]
    segments_list = outer[0] if isinstance(outer[0], list) else []
    if not segments_list:
        return meta

    seg = segments_list[0]
    if not isinstance(seg, list) or len(seg) < 10:
        return meta

    # Airline name
    if len(seg) > 1 and isinstance(seg[1], list) and seg[1]:
        meta["airline"] = seg[1][0]

    # Flight numbers: ["WN 3492"] or ["WN 2164", "WN 3158"] for connections
    if len(seg) > 2 and isinstance(seg[2], list):
        nums = []
        for fn in seg[2]:
            if isinstance(fn, list) and len(fn) >= 2:
                nums.append(f"{fn[0]} {fn[1]}")
        if nums:
            meta["flight_numbers"] = nums

    # Origin / destination
    if len(seg) > 3 and isinstance(seg[3], str):
        meta["origin"] = seg[3]
    if len(seg) > 6 and isinstance(seg[6], str):
        meta["destination"] = seg[6]

    # Departure date / time
    if len(seg) > 4 and isinstance(seg[4], list) and len(seg[4]) >= 3:
        y, m, d = seg[4]
        if all(isinstance(x, int) for x in (y, m, d)):
            meta["departure_date"] = f"{y}-{m:02d}-{d:02d}"
    if len(seg) > 5 and isinstance(seg[5], list) and seg[5]:
        h  = seg[5][0] if seg[5][0] is not None else 0
        mn = seg[5][1] if len(seg[5]) > 1 and seg[5][1] is not None else 0
        meta["departure_time"] = f"{h:02d}:{mn:02d}"

    # Arrival date / time
    if len(seg) > 7 and isinstance(seg[7], list) and len(seg[7]) >= 3:
        y, m, d = seg[7]
        if all(isinstance(x, int) for x in (y, m, d)):
            meta["arrival_date"] = f"{y}-{m:02d}-{d:02d}"
    if len(seg) > 8 and isinstance(seg[8], list) and seg[8]:
        h  = seg[8][0] if seg[8][0] is not None else 0
        mn = seg[8][1] if len(seg[8]) > 1 and seg[8][1] is not None else 0
        meta["arrival_time"] = f"{h:02d}:{mn:02d}"

    # Duration + stops
    if len(seg) > 9 and isinstance(seg[9], (int, float)):
        meta["duration_min"] = int(seg[9])
    if len(seg) > 11 and isinstance(seg[11], list):
        meta["stops"] = len(seg[11])
        meta["via"]   = seg[11]
    else:
        meta["stops"] = 0

    return meta


# ── API re-fire ────────────────────────────────────────────────────────────────

def extract_params(captured: dict) -> dict:
    """
    Parse query-string and POST body of the captured request into a flat dict
    with keys: url, f_sid, bl, at, f_req, hl, gl, soc_app, soc_platform,
    soc_device, _reqid, rt.
    """
    parsed_url = urllib.parse.urlparse(captured["url"])
    qs = urllib.parse.parse_qs(parsed_url.query)
    post_qs = urllib.parse.parse_qs(captured.get("postData", ""))

    return {
        "url":          captured["url"],
        "base_url":     f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}",
        "f_sid":        qs.get("f.sid",        [""])[0],
        "bl":           qs.get("bl",            [""])[0],
        "hl":           qs.get("hl",            ["en-US"])[0],
        "gl":           qs.get("gl",            ["US"])[0],
        "soc_app":      qs.get("soc-app",       ["162"])[0],
        "soc_platform": qs.get("soc-platform",  ["1"])[0],
        "soc_device":   qs.get("soc-device",    ["4"])[0],
        "_reqid":       qs.get("_reqid",        ["142865"])[0],
        "rt":           qs.get("rt",            ["c"])[0],
        "at":           post_qs.get("at",       [""])[0],
        "f_req":        post_qs.get("f.req",    ["[]"])[0],
    }


def build_session_headers(driver: "webdriver.Chrome") -> dict:
    """
    Extract cookies and User-Agent from the Selenium session once so they
    can be reused across multiple concurrent HTTP re-fires without calling
    Selenium from multiple threads.
    """
    cookies_str = "; ".join(
        f"{c['name']}={c['value']}"
        for c in driver.get_cookies()
    )
    return {
        "Content-Type":  "application/x-www-form-urlencoded",
        "Cookie":        cookies_str,
        "X-Same-Domain": "1",
        "Origin":        "https://www.google.com",
        "Referer":       FLIGHTS_SAVES_URL,
        "User-Agent":    driver.execute_script("return navigator.userAgent"),
    }


def fetch_prices_with_headers(params: dict, headers: dict) -> bytes:
    """
    Re-fire a single GetSolutionPrices POST with pre-built HTTP headers.
    Pure stdlib — no Selenium dependency, safe to call from threads.
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
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


# ── Price parsing ──────────────────────────────────────────────────────────────

def parse_prices(body: bytes) -> list[dict]:
    """
    Extract price records from the wrb.fr response frames.

    Returns a list of dicts, each with:
      flight_id, price, prev_price (optional), price_change (optional),
      search_url (optional).
    """
    text = body.decode("utf-8", errors="replace")

    # Strip the XSRF safety prefix
    if text.startswith(")]}'"):
        text = text[4:].lstrip("\n")

    records: dict[str, dict] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            outer = json.loads(line)
        except Exception:
            continue

        for frame in (outer if isinstance(outer, list) else [outer]):
            if not isinstance(frame, list) or len(frame) < 3:
                continue
            if frame[0] != "wrb.fr":
                continue

            try:
                inner = json.loads(frame[2])
            except Exception:
                continue

            _parse_frame(inner, records)

    return sorted(records.values(), key=lambda r: r.get("price", 0))


def _parse_frame(inner, records: dict) -> None:
    """Walk a decoded wrb.fr frame and extract (flight_id, price, ...) tuples.

    The response may nest flight records at arbitrary depth.  We recurse into
    every list item so that sub-arrays containing multiple flights are all
    visited — not just the first flight found in each top-level item.
    """
    if not isinstance(inner, list):
        return

    for item in inner:
        if not isinstance(item, list):
            continue

        flight_id   = _find_flight_id(item)
        price       = _find_price(item)
        if flight_id and price is not None:
            search_url   = _find_search_url(item)
            prev_price   = _find_nested_number(item, index=1)
            price_change = (price - prev_price) if prev_price is not None else None

            key = flight_id
            if key not in records or price < records[key].get("price", float("inf")):
                records[key] = {
                    "flight_id":    flight_id,
                    "price":        price,
                    "prev_price":   prev_price,
                    "price_change": price_change,
                    "search_url":   search_url,
                }

        # Always recurse regardless — the same item may contain nested flight
        # records that the top-level extraction above would have missed.
        _parse_frame(item, records)


def _find_flight_id(obj) -> str | None:
    """Return the first string that looks like a Google Flights saved-flight ID.

    The page uses IDs of the form  id_<32 lowercase hex chars>  (35 chars total).
    The old broad regex  [A-Za-z0-9_\\-]{30,}  also matched the long url-safe
    base64 protobuf tokens that appear throughout the response (e.g.
    "CkwKSgoDTVNZEhky..."), causing the wrong string to be returned as the ID.
    """
    if isinstance(obj, str) and re.fullmatch(r"id_[a-f0-9]{32}", obj):
        return obj
    if isinstance(obj, list):
        for item in obj:
            result = _find_flight_id(item)
            if result:
                return result
    return None


def _find_price(obj) -> int | None:
    """Return the first integer that looks like a flight price (29–50000)."""
    if isinstance(obj, (int, float)) and 29 <= obj <= 50_000:
        return int(obj)
    if isinstance(obj, list):
        for item in obj:
            result = _find_price(item)
            if result is not None:
                return result
    return None


def _find_nested_number(obj, index: int) -> int | None:
    """Return the integer at position `index` if the list contains multiple numbers."""
    if isinstance(obj, list):
        nums = [x for x in obj if isinstance(x, (int, float)) and 29 <= x <= 50_000]
        if len(nums) > index:
            return int(nums[index])
        for item in obj:
            result = _find_nested_number(item, index)
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
