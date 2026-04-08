"""
Unit tests for tracker.py — all pure-Python, no browser required.

Covers:
  - extract_params   : URL + POST body parsing
  - parse_prices     : full pipeline from raw response bytes → price records
  - _parse_frame     : price/flight-ID extraction from a decoded wrb.fr frame
  - _find_flight_id  : flight-ID heuristic
  - _find_price      : price heuristic (range guard)
  - _find_search_url : URL extraction
  - extract_flight_metadata : AF_initDataCallback HTML parsing
  - _merge_flight_ids: multi-request flight-ID deduplication
"""
import json

import pytest

from tracker import (
    extract_params,
    extract_flight_metadata,
    parse_prices,
    _find_flight_id,
    _find_price,
    _find_search_url,
    _find_nested_number,
    _parse_frame,
)
from conftest import FLIGHT_ID_1, FLIGHT_ID_2, FLIGHT_ID_SHORT, SEARCH_URL_1


# ── extract_params ─────────────────────────────────────────────────────────────

class TestExtractParams:
    def test_parses_query_string_fields(self, sample_captured_request):
        p = extract_params(sample_captured_request)
        assert p["f_sid"] == "SID123"
        assert p["bl"]    == "boq_test"
        assert p["hl"]    == "en-US"
        assert p["gl"]    == "US"
        assert p["soc_app"]      == "162"
        assert p["soc_platform"] == "1"
        assert p["soc_device"]   == "4"
        assert p["_reqid"] == "142865"
        assert p["rt"]     == "c"

    def test_parses_post_body_fields(self, sample_captured_request):
        p = extract_params(sample_captured_request)
        assert p["at"]    == "test_at_token"
        assert FLIGHT_ID_1 in p["f_req"]

    def test_base_url_strips_query_string(self, sample_captured_request):
        p = extract_params(sample_captured_request)
        assert "?" not in p["base_url"]
        assert p["base_url"].startswith("https://www.google.com")

    def test_missing_optional_fields_use_defaults(self):
        req = {
            "url": "https://www.google.com/path?hl=en-US",
            "postData": "",
        }
        p = extract_params(req)
        assert p["f_sid"] == ""
        assert p["bl"]    == ""
        assert p["at"]    == ""
        assert p["f_req"] == "[]"
        assert p["hl"]    == "en-US"


# ── parse_prices ───────────────────────────────────────────────────────────────

class TestParsePrices:
    def test_returns_list(self, sample_price_body):
        result = parse_prices(sample_price_body)
        assert isinstance(result, list)

    def test_parses_two_flights(self, sample_price_body):
        result = parse_prices(sample_price_body)
        assert len(result) == 2

    def test_strips_xsrf_prefix(self, sample_price_body):
        # The fixture body starts with )]}'
        assert sample_price_body.startswith(b")]}'")
        result = parse_prices(sample_price_body)
        assert len(result) > 0

    def test_works_without_xsrf_prefix(self, sample_price_body_no_xsrf):
        result = parse_prices(sample_price_body_no_xsrf)
        assert len(result) == 1
        assert result[0]["price"] == 300

    def test_flight1_price_and_prev_price(self, sample_price_body):
        result = parse_prices(sample_price_body)
        r1 = next(r for r in result if r["flight_id"] == FLIGHT_ID_1)
        assert r1["price"]      == 450
        assert r1["prev_price"] == 500

    def test_flight1_price_change_is_negative(self, sample_price_body):
        result = parse_prices(sample_price_body)
        r1 = next(r for r in result if r["flight_id"] == FLIGHT_ID_1)
        assert r1["price_change"] == -50

    def test_flight1_has_search_url(self, sample_price_body):
        result = parse_prices(sample_price_body)
        r1 = next(r for r in result if r["flight_id"] == FLIGHT_ID_1)
        assert "google.com/travel/flights" in r1["search_url"]

    def test_flight2_no_history(self, sample_price_body):
        result = parse_prices(sample_price_body)
        r2 = next(r for r in result if r["flight_id"] == FLIGHT_ID_2)
        assert r2["price"]        == 820
        assert r2["prev_price"]   is None
        assert r2["price_change"] is None

    def test_sorted_ascending_by_price(self, sample_price_body):
        result = parse_prices(sample_price_body)
        prices = [r["price"] for r in result]
        assert prices == sorted(prices)

    def test_deduplicates_keeps_lowest_price(self):
        """Two records for the same flight — only the cheaper one survives."""
        inner = [
            [FLIGHT_ID_1, 600, 700, SEARCH_URL_1],
            [FLIGHT_ID_1, 400, 600, SEARCH_URL_1],
        ]
        frame = json.dumps(inner)
        outer = [["wrb.fr", "key", frame, None, None]]
        body = b")]}'\n" + json.dumps(outer).encode()

        result = parse_prices(body)
        assert len(result) == 1
        assert result[0]["price"] == 400

    def test_empty_body_returns_empty_list(self):
        assert parse_prices(b"") == []

    def test_non_wrb_frames_are_ignored(self):
        outer = [["other.frame", "key", json.dumps([[FLIGHT_ID_1, 500]]), None]]
        body = json.dumps(outer).encode()
        assert parse_prices(body) == []

    def test_out_of_range_price_28_ignored(self):
        inner = [[FLIGHT_ID_1, 28]]
        frame = json.dumps(inner)
        outer = [["wrb.fr", "key", frame]]
        body = json.dumps(outer).encode()
        assert parse_prices(body) == []

    def test_out_of_range_price_50001_ignored(self):
        inner = [[FLIGHT_ID_1, 50001]]
        frame = json.dumps(inner)
        outer = [["wrb.fr", "key", frame]]
        body = json.dumps(outer).encode()
        assert parse_prices(body) == []

    def test_boundary_price_29_accepted(self):
        inner = [[FLIGHT_ID_1, 29]]
        frame = json.dumps(inner)
        outer = [["wrb.fr", "key", frame]]
        body = json.dumps(outer).encode()
        result = parse_prices(body)
        assert len(result) == 1
        assert result[0]["price"] == 29

    def test_boundary_price_50000_accepted(self):
        inner = [[FLIGHT_ID_1, 50000]]
        frame = json.dumps(inner)
        outer = [["wrb.fr", "key", frame]]
        body = json.dumps(outer).encode()
        result = parse_prices(body)
        assert len(result) == 1
        assert result[0]["price"] == 50000


# ── _find_flight_id ────────────────────────────────────────────────────────────

class TestFindFlightId:
    def test_accepts_valid_flight_id(self):
        assert _find_flight_id(FLIGHT_ID_1) == FLIGHT_ID_1

    def test_rejects_short_string(self):
        assert _find_flight_id(FLIGHT_ID_SHORT) is None

    def test_rejects_missing_prefix(self):
        assert _find_flight_id("aaaabbbbccccddddeeeeffffgggghhhh") is None

    def test_rejects_uppercase_hex(self):
        assert _find_flight_id("id_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH") is None

    def test_rejects_wrong_hex_length(self):
        assert _find_flight_id("id_aaaabbbbccccddddeeeeffffgggghhh") is None  # 31 hex

    def test_finds_id_nested_in_list(self):
        assert _find_flight_id([None, "short", FLIGHT_ID_1]) == FLIGHT_ID_1

    def test_finds_id_in_deeply_nested_list(self):
        nested = [[[None, [FLIGHT_ID_2]]]]
        assert _find_flight_id(nested) == FLIGHT_ID_2

    def test_returns_none_for_integer(self):
        assert _find_flight_id(12345) is None

    def test_returns_none_for_none(self):
        assert _find_flight_id(None) is None

    def test_rejects_string_with_spaces(self):
        assert _find_flight_id("id_ aaabbbbccccddddeeeeffffgggghhhh") is None


# ── _find_price ────────────────────────────────────────────────────────────────

class TestFindPrice:
    def test_accepts_price_in_range(self):
        assert _find_price(500) == 500

    def test_boundary_low_29(self):
        assert _find_price(29) == 29

    def test_boundary_high_50000(self):
        assert _find_price(50000) == 50000

    def test_rejects_28(self):
        assert _find_price(28) is None

    def test_rejects_50001(self):
        assert _find_price(50001) is None

    def test_rejects_zero(self):
        assert _find_price(0) is None

    def test_rejects_negative(self):
        assert _find_price(-100) is None

    def test_finds_price_in_list(self):
        assert _find_price([FLIGHT_ID_1, 450, "url"]) == 450

    def test_skips_out_of_range_finds_valid(self):
        # 10 is out of range, 300 is valid
        assert _find_price([10, 300]) == 300

    def test_returns_integer_even_for_float_input(self):
        assert _find_price(499.99) == 499
        assert isinstance(_find_price(499.99), int)


# ── _find_search_url ───────────────────────────────────────────────────────────

class TestFindSearchUrl:
    def test_finds_flights_url_string(self):
        assert _find_search_url(SEARCH_URL_1) == SEARCH_URL_1

    def test_finds_url_nested_in_list(self):
        assert _find_search_url([FLIGHT_ID_1, 500, SEARCH_URL_1]) == SEARCH_URL_1

    def test_returns_none_for_non_flights_url(self):
        assert _find_search_url("https://www.google.com/maps") is None

    def test_returns_none_for_integer(self):
        assert _find_search_url(12345) is None


# ── _find_nested_number ────────────────────────────────────────────────────────

class TestFindNestedNumber:
    def test_returns_second_number_at_index_1(self):
        assert _find_nested_number([FLIGHT_ID_1, 450, 500], index=1) == 500

    def test_returns_none_when_only_one_number(self):
        assert _find_nested_number([FLIGHT_ID_1, 450], index=1) is None

    def test_out_of_range_numbers_not_counted(self):
        # 10 is out of range (< 29), 450 and 500 are valid → index=1 gives 500
        assert _find_nested_number([10, 450, 500], index=1) == 500


# ── extract_flight_metadata ────────────────────────────────────────────────────

class TestExtractFlightMetadata:
    def test_returns_dict_keyed_by_flight_id(self, sample_page_html):
        result = extract_flight_metadata(sample_page_html)
        assert FLIGHT_ID_1 in result

    def test_extracts_route_fields(self, sample_page_html):
        meta = extract_flight_metadata(sample_page_html)[FLIGHT_ID_1]
        assert meta["origin"] == "ORD"
        assert meta["destination"] == "LAX"
        assert meta["airline"] == "United Airlines"
        assert meta["departure_time"] == "08:30"

    def test_extracts_search_url(self, sample_page_html):
        meta = extract_flight_metadata(sample_page_html)[FLIGHT_ID_1]
        assert "google.com" in meta.get("search_url", "")

    def test_empty_html_returns_empty_dict(self):
        assert extract_flight_metadata("") == {}

    def test_html_without_callbacks_returns_empty_dict(self):
        assert extract_flight_metadata("<html><body>no scripts</body></html>") == {}
