"""
Saved-flight metadata parsing.

The saves page embeds flight data in AF_initDataCallback script blocks as
positional arrays (no named keys). We parse the confirmed positions and
recursively search for entries so layout shuffles at other depths don't
break us.

Returned metadata dict per flight:
  airline, flight_numbers, origin, destination, departure_date,
  departure_time, arrival_date, arrival_time, duration_min, stops,
  via (stopover airports), search_url,
  slices — one entry per journey slice (outbound / return); the top-level
           fields mirror slices[0] so single-leg display code stays simple.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

FLIGHT_ID_RE = re.compile(r"id_[a-f0-9]{32}")


def extract_flight_metadata(page_html: str) -> dict[str, dict]:
    """Parse all AF_initDataCallback blocks; returns {flight_id: meta}."""
    metadata: dict[str, dict] = {}

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
        if isinstance(payload, list):
            # Flight entries nest at varying depths across blocks —
            # recursively search rather than assuming a fixed path.
            _find_flight_entries(payload, metadata)

    log.info("Metadata extracted for %d flight(s)", len(metadata))
    return metadata


def _extract_js_data_array(block: str) -> list | None:
    """
    Pull the value of the 'data:' key from a JavaScript object-literal string
    and parse it as JSON.

    AF_initDataCallback({key: 'ds:1', hash: '5', data: [...], sideChannel: {}})
                                                        ^^^^^ this part

    We locate the opening '[' via regex, then walk the string to find the
    matching ']' (the data value itself is standard JSON).
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
                    return json.loads(block[start: start + i + 1])
                except ValueError:
                    return None
    return None


def _find_flight_entries(obj, results: dict) -> None:
    """
    Recursively search a nested list structure for [flight_id, details]
    entries and populate `results` with parsed metadata.
    """
    if not isinstance(obj, list):
        return

    # Check if *this* list is a flight entry: [id_xxx, details_array, ...]
    if (
        len(obj) >= 2
        and isinstance(obj[0], str)
        and FLIGHT_ID_RE.fullmatch(obj[0])
        and isinstance(obj[1], list)
    ):
        meta = _parse_flight_details(obj[1])
        if meta:
            results[obj[0]] = meta
        # Don't recurse into a matched entry — its details array contains
        # numbers/strings that would confuse the search.
        return

    for item in obj:
        if isinstance(item, list):
            _find_flight_entries(item, results)


def _parse_flight_details(details: list) -> dict:
    """
    Parse one flight's positional details array.

    Confirmed positions (from page inspection):
      [1]  relative search URL  (/travel/flights?tfs=...)
      [4]  [ [segment_row, ...], ["Airline Name"] ]

    Each segment_row is one journey slice (outbound, return, ...).
    """
    meta: dict = {}

    # Search URL
    if len(details) > 1 and isinstance(details[1], str) and "/travel/flights" in details[1]:
        url = details[1].split("&authuser")[0]
        meta["search_url"] = f"https://www.google.com{url}" if url.startswith("/") else url

    if len(details) <= 4 or not isinstance(details[4], list) or not details[4]:
        return meta

    outer = details[4]
    segment_rows = outer[0] if isinstance(outer[0], list) else []

    slices = [
        parsed for row in segment_rows
        if isinstance(row, list) and (parsed := _parse_segment_row(row))
    ]
    if not slices:
        return meta

    # Top-level fields mirror the first slice; keep all slices for round trips.
    meta.update(slices[0])
    if len(slices) > 1:
        meta["slices"] = slices

    return meta


def _parse_segment_row(seg: list) -> dict:
    """
    Parse one segment row (one journey slice).

    Confirmed positions:
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
    if len(seg) < 10:
        return {}

    slice_meta: dict = {}

    # Airline name
    if isinstance(seg[1], list) and seg[1]:
        slice_meta["airline"] = seg[1][0]

    # Flight numbers: ["WN 3492"] or ["WN 2164", "WN 3158"] for connections
    if isinstance(seg[2], list):
        nums = [
            f"{fn[0]} {fn[1]}"
            for fn in seg[2]
            if isinstance(fn, list) and len(fn) >= 2
        ]
        if nums:
            slice_meta["flight_numbers"] = nums

    # Origin / destination
    if isinstance(seg[3], str):
        slice_meta["origin"] = seg[3]
    if len(seg) > 6 and isinstance(seg[6], str):
        slice_meta["destination"] = seg[6]

    # Departure date / time
    dep_date = _parse_ymd(seg[4])
    if dep_date:
        slice_meta["departure_date"] = dep_date
    if len(seg) > 5:
        dep_time = _parse_hm(seg[5])
        if dep_time:
            slice_meta["departure_time"] = dep_time

    # Arrival date / time
    if len(seg) > 7:
        arr_date = _parse_ymd(seg[7])
        if arr_date:
            slice_meta["arrival_date"] = arr_date
    if len(seg) > 8:
        arr_time = _parse_hm(seg[8])
        if arr_time:
            slice_meta["arrival_time"] = arr_time

    # Duration + stops
    if len(seg) > 9 and isinstance(seg[9], (int, float)):
        slice_meta["duration_min"] = int(seg[9])
    if len(seg) > 11 and isinstance(seg[11], list):
        slice_meta["stops"] = len(seg[11])
        slice_meta["via"] = seg[11]
    else:
        slice_meta["stops"] = 0

    return slice_meta


def _parse_ymd(value) -> str | None:
    """[2026, 4, 1] → '2026-04-01'."""
    if (
        isinstance(value, list) and len(value) >= 3
        and all(isinstance(x, int) for x in value[:3])
    ):
        y, m, d = value[:3]
        return f"{y}-{m:02d}-{d:02d}"
    return None


def _parse_hm(value) -> str | None:
    """[8, 30] → '08:30'; [14] → '14:00'; None hours/minutes → 0."""
    if not isinstance(value, list) or not value:
        return None
    hours = value[0] if isinstance(value[0], int) else 0
    minutes = value[1] if len(value) > 1 and isinstance(value[1], int) else 0
    return f"{hours:02d}:{minutes:02d}"
