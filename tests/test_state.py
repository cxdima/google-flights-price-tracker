"""Unit tests for gfpt.storage.state and gfpt.storage.dynamo — mocked AWS."""
import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from conftest import FLIGHT_ID_1, SEARCH_URL_1
from gfpt.models import PriceQuote
from gfpt.storage.dynamo import PriceHistory
from gfpt.storage.state import StateStore


def _s3_with_json(payload) -> MagicMock:
    """S3 client whose get_object returns the given payload as JSON."""
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode()
    s3.get_object.return_value = {"Body": body}
    return s3


def _store(s3) -> StateStore:
    return StateStore("test-bucket", s3_client=s3)


# ── Reads degrade to defaults ──────────────────────────────────────────────────

class TestReadDefaults:
    def test_load_session_returns_none_when_key_missing(self, mock_s3_empty):
        assert _store(mock_s3_empty).load_session() is None

    def test_load_manifest_returns_empty_dict_when_key_missing(self, mock_s3_empty):
        assert _store(mock_s3_empty).load_manifest() == {}

    def test_load_summary_returns_none_when_key_missing(self, mock_s3_empty):
        assert _store(mock_s3_empty).load_summary() is None

    def test_load_user_prefs_returns_empty_dict_when_key_missing(self, mock_s3_empty):
        assert _store(mock_s3_empty).load_user_prefs() == {}

    def test_load_failure_streak_returns_zero_when_key_missing(self, mock_s3_empty):
        assert _store(mock_s3_empty).load_failure_streak() == 0

    def test_unexpected_client_error_still_returns_default(self):
        s3 = MagicMock()
        s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}}, "GetObject",
        )

        assert _store(s3).load_manifest() == {}

    def test_non_client_exception_returns_default(self):
        s3 = MagicMock()
        s3.get_object.side_effect = ConnectionError("network down")

        assert _store(s3).load_manifest() == {}

    def test_corrupt_json_returns_default(self):
        s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b"{not json"
        s3.get_object.return_value = {"Body": body}

        assert _store(s3).load_manifest() == {}

    def test_non_dict_manifest_normalized_to_empty_dict(self):
        assert _store(_s3_with_json([1, 2, 3])).load_manifest() == {}

    def test_non_dict_user_prefs_normalized_to_empty_dict(self):
        assert _store(_s3_with_json("garbage")).load_user_prefs() == {}


# ── Successful reads ───────────────────────────────────────────────────────────

class TestReads:
    def test_load_manifest_returns_stored_dict(self):
        manifest = {FLIGHT_ID_1: {"price": 450}, "__updated__": "ts"}

        assert _store(_s3_with_json(manifest)).load_manifest() == manifest

    def test_load_session_returns_stored_payload(self):
        payload = {"saved_at": "2026-07-08T00:00:00+00:00", "cookies": []}

        assert _store(_s3_with_json(payload)).load_session() == payload

    def test_load_failure_streak_reads_counter(self):
        s3 = _s3_with_json({"consecutive_failures": 4})

        assert _store(s3).load_failure_streak() == 4

    def test_load_failure_streak_coerces_string_counter(self):
        s3 = _s3_with_json({"consecutive_failures": "7"})

        assert _store(s3).load_failure_streak() == 7

    def test_load_failure_streak_handles_non_dict_health(self):
        assert _store(_s3_with_json([1, 2])).load_failure_streak() == 0

    def test_load_failure_streak_handles_non_numeric_counter(self):
        s3 = _s3_with_json({"consecutive_failures": "not-a-number"})

        assert _store(s3).load_failure_streak() == 0


# ── Writes ─────────────────────────────────────────────────────────────────────

class TestWrites:
    def test_save_manifest_puts_json_to_bucket(self):
        s3 = MagicMock()
        manifest = {FLIGHT_ID_1: {"price": 450}}

        assert _store(s3).save_manifest(manifest) is True

        kwargs = s3.put_object.call_args.kwargs
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == "flights/manifest.json"
        assert json.loads(kwargs["Body"]) == manifest
        assert kwargs["ContentType"] == "application/json"

    def test_save_session_uses_session_key(self):
        s3 = MagicMock()

        _store(s3).save_session({"cookies": []})

        assert s3.put_object.call_args.kwargs["Key"] == "sessions/latest.json"

    def test_save_health_writes_dict_shape(self):
        s3 = MagicMock()

        _store(s3).save_health({"consecutive_failures": 3, "failure_notified": True})

        body = json.loads(s3.put_object.call_args.kwargs["Body"])
        assert body == {"consecutive_failures": 3, "failure_notified": True}

    def test_write_failure_returns_false_and_does_not_raise(self):
        s3 = MagicMock()
        s3.put_object.side_effect = Exception("connection reset")

        assert _store(s3).save_manifest({}) is False

    def test_save_screenshot_returns_png_key(self):
        s3 = MagicMock()

        key = _store(s3).save_screenshot("auth-fail", b"\x89PNG")

        assert key.startswith("screenshots/auth-fail-")
        assert key.endswith(".png")
        assert s3.put_object.call_args.kwargs["ContentType"] == "image/png"

    def test_save_screenshot_returns_none_on_upload_error(self):
        s3 = MagicMock()
        s3.put_object.side_effect = Exception("denied")

        assert _store(s3).save_screenshot("label", b"png") is None


# ── Empty bucket short-circuit ─────────────────────────────────────────────────

class TestEmptyBucket:
    @pytest.fixture
    def bucketless(self):
        s3 = MagicMock()
        return StateStore("", s3_client=s3), s3

    def test_reads_return_defaults_without_touching_s3(self, bucketless):
        store, s3 = bucketless

        assert store.load_session() is None
        assert store.load_manifest() == {}
        assert store.load_summary() is None
        assert store.load_user_prefs() == {}
        assert store.load_failure_streak() == 0
        s3.get_object.assert_not_called()

    def test_writes_return_false_without_touching_s3(self, bucketless):
        store, s3 = bucketless

        assert store.save_manifest({}) is False
        assert store.save_session({}) is False
        assert store.save_health({"consecutive_failures": 1}) is False
        assert store.save_screenshot("x", b"png") is None
        s3.put_object.assert_not_called()


# ── PriceHistory (DynamoDB) ────────────────────────────────────────────────────

class TestPriceHistory:
    def test_last_price_returns_none_when_no_history(
        self, mock_dynamodb_resource, mock_dynamodb_table
    ):
        history = PriceHistory("test-prices", dynamodb=mock_dynamodb_resource)

        assert history.last_price(FLIGHT_ID_1) is None
        mock_dynamodb_table.query.assert_called_once()

    def test_last_price_returns_int_from_decimal_item(
        self, mock_dynamodb_resource, mock_dynamodb_table
    ):
        mock_dynamodb_table.query.return_value = {"Items": [{"price": Decimal("450")}]}
        history = PriceHistory("test-prices", dynamodb=mock_dynamodb_resource)

        price = history.last_price(FLIGHT_ID_1)

        assert price == 450
        assert isinstance(price, int)

    def test_last_price_raises_lookup_error_when_query_fails(
        self, mock_dynamodb_resource, mock_dynamodb_table
    ):
        # "couldn't read" must never look like "never seen" — that would
        # re-announce an existing flight and reset its low watermark.
        mock_dynamodb_table.query.side_effect = Exception("throttled")
        history = PriceHistory("test-prices", dynamodb=mock_dynamodb_resource)

        with pytest.raises(LookupError):
            history.last_price(FLIGHT_ID_1)

    def test_record_puts_item_with_core_fields(
        self, mock_dynamodb_resource, mock_dynamodb_table
    ):
        history = PriceHistory("test-prices", dynamodb=mock_dynamodb_resource)
        quote = PriceQuote(FLIGHT_ID_1, 450, SEARCH_URL_1)

        assert history.record(quote, {"origin": "ORD"}, prev_price=500) is True

        item = mock_dynamodb_table.put_item.call_args.kwargs["Item"]
        assert item["flight_id"] == FLIGHT_ID_1
        assert item["price"] == 450
        assert item["prev_price"] == 500
        assert item["search_url"] == SEARCH_URL_1
        assert item["origin"] == "ORD"
        assert item["ttl"] > item["ts"] // 1000

    def test_record_copies_only_known_meta_fields(
        self, mock_dynamodb_resource, mock_dynamodb_table
    ):
        history = PriceHistory("test-prices", dynamodb=mock_dynamodb_resource)
        meta = {"origin": "ORD", "unknown_field": "must not be copied"}

        history.record(PriceQuote(FLIGHT_ID_1, 450), meta)

        item = mock_dynamodb_table.put_item.call_args.kwargs["Item"]
        assert "unknown_field" not in item

    def test_record_falls_back_to_meta_search_url(
        self, mock_dynamodb_resource, mock_dynamodb_table
    ):
        history = PriceHistory("test-prices", dynamodb=mock_dynamodb_resource)

        history.record(PriceQuote(FLIGHT_ID_1, 450), {"search_url": SEARCH_URL_1})

        item = mock_dynamodb_table.put_item.call_args.kwargs["Item"]
        assert item["search_url"] == SEARCH_URL_1

    def test_record_returns_false_and_does_not_raise_on_put_error(
        self, mock_dynamodb_resource, mock_dynamodb_table
    ):
        mock_dynamodb_table.put_item.side_effect = Exception("throttled")
        history = PriceHistory("test-prices", dynamodb=mock_dynamodb_resource)

        assert history.record(PriceQuote(FLIGHT_ID_1, 450)) is False

    def test_table_resolved_by_name(self, mock_dynamodb_resource):
        PriceHistory("test-prices", dynamodb=mock_dynamodb_resource)

        mock_dynamodb_resource.Table.assert_called_once_with("test-prices")
