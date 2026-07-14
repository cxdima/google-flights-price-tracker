"""Unit tests for gfpt.tracking.prices — response parsing, no network."""
import json
import urllib.request

import pytest

from conftest import FLIGHT_ID_1, FLIGHT_ID_2, FLIGHT_ID_SHORT, SEARCH_URL_1
from gfpt.models import PriceQuote
from gfpt.tracking.prices import (
    _find_flight_id,
    _find_price,
    _find_search_url,
    extract_params,
    fetch_prices,
    parse_prices,
)


def _body(inner, frame_key="wrb.fr", prefix=False) -> bytes:
    """Wrap an inner payload in the wrb.fr response envelope."""
    outer = [[frame_key, "GetSolutionPrices", json.dumps(inner), None, None]]
    raw = json.dumps(outer).encode()
    return (b")]}'\n" + raw) if prefix else raw


# ── extract_params ─────────────────────────────────────────────────────────────

class TestExtractParams:
    def test_parses_query_string_fields(self, sample_captured_request):
        p = extract_params(sample_captured_request)

        assert p["f_sid"] == "SID123"
        assert p["bl"] == "boq_test"
        assert p["hl"] == "en-US"
        assert p["gl"] == "US"
        assert p["soc_app"] == "162"
        assert p["soc_platform"] == "1"
        assert p["soc_device"] == "4"
        assert p["_reqid"] == "142865"
        assert p["rt"] == "c"

    def test_parses_post_body_fields(self, sample_captured_request):
        p = extract_params(sample_captured_request)

        assert p["at"] == "test_at_token"
        assert FLIGHT_ID_1 in p["f_req"]

    def test_base_url_strips_query_string(self, sample_captured_request):
        p = extract_params(sample_captured_request)

        assert "?" not in p["base_url"]
        assert p["base_url"].startswith("https://www.google.com")

    def test_missing_optional_fields_use_defaults(self):
        req = {"url": "https://www.google.com/path?hl=en-US", "postData": ""}

        p = extract_params(req)

        assert p["f_sid"] == ""
        assert p["bl"] == ""
        assert p["at"] == ""
        assert p["f_req"] == "[]"
        assert p["hl"] == "en-US"


# ── parse_prices ───────────────────────────────────────────────────────────────

class TestParsePrices:
    def test_returns_list_of_price_quotes(self, sample_price_body):
        result = parse_prices(sample_price_body)

        assert isinstance(result, list)
        assert all(isinstance(q, PriceQuote) for q in result)

    def test_parses_two_flights(self, sample_price_body):
        result = parse_prices(sample_price_body)

        assert {q.flight_id for q in result} == {FLIGHT_ID_1, FLIGHT_ID_2}

    def test_strips_xsrf_prefix(self, sample_price_body):
        assert sample_price_body.startswith(b")]}'")

        result = parse_prices(sample_price_body)

        assert len(result) == 2

    def test_works_without_xsrf_prefix(self, sample_price_body_no_xsrf):
        result = parse_prices(sample_price_body_no_xsrf)

        assert len(result) == 1
        assert result[0].price == 300

    def test_flight1_price_and_search_url(self, sample_price_body):
        result = parse_prices(sample_price_body)

        q1 = next(q for q in result if q.flight_id == FLIGHT_ID_1)
        assert q1.price == 450
        assert "google.com/travel/flights" in q1.search_url

    def test_flight_without_url_has_none_search_url(self, sample_price_body):
        result = parse_prices(sample_price_body)

        q2 = next(q for q in result if q.flight_id == FLIGHT_ID_2)
        assert q2.price == 820
        assert q2.search_url is None

    def test_sorted_ascending_by_price(self, sample_price_body):
        result = parse_prices(sample_price_body)

        prices = [q.price for q in result]
        assert prices == sorted(prices)

    def test_deduplicates_keeps_lowest_price(self):
        inner = [
            [FLIGHT_ID_1, 600, SEARCH_URL_1],
            [FLIGHT_ID_1, 400, SEARCH_URL_1],
        ]

        result = parse_prices(_body(inner, prefix=True))

        assert len(result) == 1
        assert result[0].price == 400

    def test_empty_body_returns_empty_list(self):
        assert parse_prices(b"") == []

    def test_non_wrb_frames_are_ignored(self):
        assert parse_prices(_body([[FLIGHT_ID_1, 500]], frame_key="other.frame")) == []

    def test_frame_with_non_string_payload_is_skipped(self):
        outer = [["wrb.fr", "GetSolutionPrices", None, None]]

        assert parse_prices(json.dumps(outer).encode()) == []

    def test_out_of_range_price_28_ignored(self):
        assert parse_prices(_body([[FLIGHT_ID_1, 28]])) == []

    def test_out_of_range_price_50001_ignored(self):
        assert parse_prices(_body([[FLIGHT_ID_1, 50001]])) == []

    def test_boundary_price_29_accepted(self):
        result = parse_prices(_body([[FLIGHT_ID_1, 29]]))

        assert len(result) == 1
        assert result[0].price == 29

    def test_boundary_price_50000_accepted(self):
        result = parse_prices(_body([[FLIGHT_ID_1, 50000]]))

        assert len(result) == 1
        assert result[0].price == 50000

    def test_nested_flights_at_different_depths_all_found(self):
        inner = [[[FLIGHT_ID_1, 450]], [[[FLIGHT_ID_2, 820]]]]

        result = parse_prices(_body(inner))

        assert {q.flight_id for q in result} == {FLIGHT_ID_1, FLIGHT_ID_2}


# ── _find_flight_id ────────────────────────────────────────────────────────────

class TestFindFlightId:
    def test_accepts_valid_flight_id(self):
        assert _find_flight_id(FLIGHT_ID_1) == FLIGHT_ID_1

    def test_rejects_short_string(self):
        assert _find_flight_id(FLIGHT_ID_SHORT) is None

    def test_rejects_missing_prefix(self):
        assert _find_flight_id("aaaabbbbccccddddeeeeffffaaaabbbb") is None

    def test_rejects_uppercase_hex(self):
        assert _find_flight_id("id_AAAABBBBCCCCDDDDEEEEFFFFAAAABBBB") is None

    def test_rejects_wrong_hex_length(self):
        assert _find_flight_id("id_aaaabbbbccccddddeeeeffffaaabbb") is None  # 31 hex

    def test_finds_id_nested_in_list(self):
        assert _find_flight_id([None, "short", FLIGHT_ID_1]) == FLIGHT_ID_1

    def test_finds_id_in_deeply_nested_list(self):
        assert _find_flight_id([[[None, [FLIGHT_ID_2]]]]) == FLIGHT_ID_2

    def test_returns_none_for_integer(self):
        assert _find_flight_id(12345) is None

    def test_returns_none_for_none(self):
        assert _find_flight_id(None) is None

    def test_rejects_string_with_spaces(self):
        assert _find_flight_id("id_ aaabbbbccccddddeeeeffffaaaabbbb") is None


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

    def test_rejects_bool_true(self):
        # bool is a subclass of int — True must NOT parse as a price
        assert _find_price(True) is None

    def test_skips_bool_and_finds_real_price_in_list(self):
        assert _find_price([True, 300]) == 300

    def test_finds_price_in_list(self):
        assert _find_price([FLIGHT_ID_1, 450, "url"]) == 450

    def test_skips_out_of_range_finds_valid(self):
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


# ── fetch_prices retry behaviour ───────────────────────────────────────────────
# Google's bot defense tarpits ~35% of re-fires from AWS egress IPs
# (per-attempt random) — a retry on a fresh connection re-rolls the dice.

def _refire_params() -> dict:
    return {
        "base_url": "https://www.google.com/batchexecute",
        "f_sid": "1", "bl": "b", "hl": "en", "gl": "US",
        "soc_app": "162", "soc_platform": "1", "soc_device": "1",
        "_reqid": "1", "rt": "c", "at": "tok", "f_req": "[]",
    }


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestFetchPricesRetries:
    def test_retries_after_timeout_and_returns_second_response(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise TimeoutError("The read operation timed out")
            return _FakeResponse(b"payload")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        assert fetch_prices(_refire_params(), {}, attempts=3) == b"payload"
        assert len(calls) == 2

    def test_single_attempt_by_default(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(1)
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(TimeoutError):
            fetch_prices(_refire_params(), {})
        assert len(calls) == 1

    def test_raises_last_error_after_exhausting_attempts(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(1)
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(TimeoutError):
            fetch_prices(_refire_params(), {}, attempts=3)
        assert len(calls) == 3

    def test_retries_stop_when_deadline_passed(self, monkeypatch):
        """The retry budget is wall-clock shared across a batch — a past
        deadline means the first attempt still runs, but no retries."""
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(1)
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        import time as _time

        with pytest.raises(TimeoutError):
            fetch_prices(_refire_params(), {}, attempts=3,
                         deadline=_time.monotonic() - 1)
        assert len(calls) == 1
