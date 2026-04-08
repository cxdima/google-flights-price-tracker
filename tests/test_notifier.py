"""
Unit tests for notifier.py — no network calls, no browser.

Covers:
  - build_price_message : headline, route, carrier, price formatting
  - _h                  : HTML character escaping
  - handle_webhook      : routing, auth guard, /status dispatch
  - build_flights_message : /flights listing
"""
import json
from unittest.mock import patch, MagicMock

import pytest

import notifier
from notifier import (
    build_price_message,
    handle_webhook,
    _h,
    build_flights_message,
)
from conftest import FLIGHT_ID_1, SEARCH_URL_1


# ── Helpers ────────────────────────────────────────────────────────────────────

def _record(price, prev_price=None, price_change=None, search_url=None):
    return {
        "flight_id":    FLIGHT_ID_1,
        "price":        price,
        "prev_price":   prev_price,
        "price_change": price_change,
        "search_url":   search_url or SEARCH_URL_1,
    }


def _meta(origin="ORD", dest="LAX", airline="United Airlines",
          flight_numbers=None, dep_t="08:30", arr_t="11:45",
          dep_d="2026-04-01"):
    return {
        "origin":          origin,
        "destination":     dest,
        "airline":         airline,
        "flight_numbers":  flight_numbers or ["UA 100"],
        "departure_time":  dep_t,
        "arrival_time":    arr_t,
        "departure_date":  dep_d,
    }


# ── build_price_message ────────────────────────────────────────────────────────

class TestBuildPriceMessage:
    def test_new_flight_uses_tracking_text(self):
        msg = build_price_message(_record(500), {}, is_new_flight=True)
        assert "Now tracking" in msg

    def test_price_drop_shows_new_low(self):
        msg = build_price_message(_record(400), {}, last_known_price=500)
        assert "New low" in msg

    def test_message_contains_price(self):
        msg = build_price_message(_record(1_234), {}, is_new_flight=True)
        assert "1,234" in msg

    def test_route_appears_with_metadata(self):
        msg = build_price_message(_record(500), _meta(), is_new_flight=True)
        assert "ORD" in msg
        assert "LAX" in msg

    def test_airline_appears(self):
        msg = build_price_message(_record(500), _meta(), is_new_flight=True)
        assert "United Airlines" in msg

    def test_no_route_without_metadata(self):
        msg = build_price_message(_record(500), {}, is_new_flight=True)
        assert "ORD" not in msg


# ── _h (HTML escape) ──────────────────────────────────────────────────────────

class TestHtmlEscape:
    def test_escapes_ampersand(self):
        assert _h("A & B") == "A &amp; B"

    def test_escapes_angle_brackets(self):
        assert _h("<script>") == "&lt;script&gt;"

    def test_plain_text_unchanged(self):
        assert _h("hello world") == "hello world"

    def test_dollar_sign_unchanged(self):
        assert _h("$500") == "$500"


# ── handle_webhook ─────────────────────────────────────────────────────────────

class TestHandleWebhook:
    def _make_payload(self, chat_id, text="/status"):
        return json.dumps({
            "update_id": 1,
            "message": {
                "chat": {"id": chat_id},
                "text": text,
            }
        })

    def test_invalid_json_returns_error(self):
        result = handle_webhook("not-json")
        assert result["ok"] is False
        assert "error" in result

    def test_unauthorized_chat_is_ignored(self):
        payload = self._make_payload(chat_id=99999999)
        result  = handle_webhook(payload)
        assert result.get("ignored") is True

    def test_authorized_chat_status_dispatches(self):
        authorized_id = int(notifier._AUTHORIZED_IDS.__iter__().__next__())
        payload = self._make_payload(chat_id=authorized_id)
        summary = {
            "ok": True, "flights": 2, "updated": 1,
            "runtime_secs": 30, "ts": "03/15/2026 10:00",
        }
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            result = handle_webhook(payload, last_run_summary=summary)
        assert result["ok"] is True
        mock_send.assert_called_once()

    def test_authorized_chat_no_summary_still_responds(self):
        authorized_id = int(notifier._AUTHORIZED_IDS.__iter__().__next__())
        payload = self._make_payload(chat_id=authorized_id)
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            result = handle_webhook(payload, last_run_summary=None)
        assert result["ok"] is True
        mock_send.assert_called_once()

    def test_non_status_command_not_dispatched(self):
        authorized_id = int(notifier._AUTHORIZED_IDS.__iter__().__next__())
        payload = self._make_payload(chat_id=authorized_id, text="/help")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            handle_webhook(payload)
        mock_send.assert_not_called()

    def test_edited_message_is_handled(self):
        authorized_id = int(notifier._AUTHORIZED_IDS.__iter__().__next__())
        payload = json.dumps({
            "update_id": 2,
            "edited_message": {
                "chat": {"id": authorized_id},
                "text": "/status",
            }
        })
        with patch.object(notifier, "_send", return_value=True):
            result = handle_webhook(payload)
        assert result["ok"] is True


# ── build_flights_message ──────────────────────────────────────────────────────

class TestBuildFlightsMessage:
    def test_empty_manifest(self):
        msg = build_flights_message({})
        assert "No flights" in msg

    def test_shows_flight_count(self):
        manifest = {
            FLIGHT_ID_1: {
                "origin": "ORD", "destination": "LAX",
                "departure_date": "2026-04-01",
                "flight_numbers": ["UA 100"],
                "price": 450,
            }
        }
        msg = build_flights_message(manifest)
        assert "1" in msg
        assert "ORD" in msg
        assert "LAX" in msg

    def test_skips_metadata_keys(self):
        manifest = {"__updated__": "2026-04-07 12:00"}
        msg = build_flights_message(manifest)
        assert "No flights" in msg
