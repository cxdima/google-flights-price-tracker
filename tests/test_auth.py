"""
Unit tests for auth.py — no browser or AWS calls required.

Covers:
  - _is_authenticated   : URL and DOM-based login detection
  - _try_restore_session: S3 loading, age check, cookie injection
  - _save_session       : S3 write, no-op without bucket
"""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytest
from selenium.common.exceptions import NoSuchElementException

from auth import _is_authenticated, _try_restore_session, _save_session
import config as _config


# ── Helpers ────────────────────────────────────────────────────────────────────

def _driver(url="https://www.google.com/travel/flights/saves"):
    """Create a minimal mock Selenium driver."""
    d = MagicMock()
    d.current_url = url
    # Default: no sign-in link found (raises NoSuchElementException)
    d.find_element.side_effect = NoSuchElementException()
    return d


def _s3_with_payload(payload_dict: dict):
    """Return a mock S3 client whose get_object returns the given payload."""
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = json.dumps(payload_dict).encode()
    s3.get_object.return_value = {"Body": body}
    return s3


def _s3_empty():
    """S3 client that raises NoSuchKey on every get_object call."""
    from botocore.exceptions import ClientError
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "GetObject",
    )
    return s3


# ── _is_authenticated ──────────────────────────────────────────────────────────

class TestIsAuthenticated:
    def test_false_on_accounts_google(self):
        d = _driver("https://accounts.google.com/signin/v2/identifier")
        assert _is_authenticated(d) is False

    def test_false_on_myaccount_google(self):
        d = _driver("https://myaccount.google.com/intro/passkey")
        assert _is_authenticated(d) is False

    def test_false_when_signin_link_visible(self):
        d = _driver("https://www.google.com/travel/flights/saves")
        d.find_element.side_effect = None          # element found
        d.find_element.return_value = MagicMock()  # a real element
        assert _is_authenticated(d) is False

    def test_true_on_flights_saves_page(self):
        d = _driver("https://www.google.com/travel/flights/saves")
        # find_element raises → no sign-in link → logged in
        d.find_element.side_effect = NoSuchElementException()
        assert _is_authenticated(d) is True

    def test_true_on_any_google_non_accounts_url(self):
        d = _driver("https://www.google.com/travel/flights")
        d.find_element.side_effect = NoSuchElementException()
        assert _is_authenticated(d) is True


# ── _try_restore_session ───────────────────────────────────────────────────────

class TestTryRestoreSession:
    def test_returns_false_without_bucket(self):
        """No S3 bucket configured → never attempt restore."""
        original = _config.S3_BUCKET
        try:
            _config.S3_BUCKET = ""
            d = _driver()
            assert _try_restore_session(d, MagicMock()) is False
        finally:
            _config.S3_BUCKET = original

    def test_returns_false_when_s3_raises(self):
        d = _driver()
        assert _try_restore_session(d, _s3_empty()) is False

    def test_returns_false_for_expired_session(self):
        """Session saved more than SESSION_MAX_AGE_SECS ago → force fresh login."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        payload = {"saved_at": old_time, "cookies": []}
        d = _driver()
        assert _try_restore_session(d, _s3_with_payload(payload)) is False

    def test_injects_cookies_from_s3(self):
        """Valid session → cookies should be injected via CDP."""
        fresh_time = datetime.now(timezone.utc).isoformat()
        cookies = [
            {"name": "SID", "value": "fake-sid", "domain": ".google.com", "path": "/"},
        ]
        payload = {"saved_at": fresh_time, "cookies": cookies}
        s3 = _s3_with_payload(payload)

        d = _driver()
        # After cookies are injected, navigating to saves should keep us logged in
        d.current_url = "https://www.google.com/travel/flights/saves"
        d.find_element.side_effect = NoSuchElementException()

        result = _try_restore_session(d, s3)

        # Cookies are injected via CDP Network.setCookie
        d.execute_cdp_cmd.assert_called()
        assert result is True

    def test_sameSite_passed_through_to_cdp(self):
        """sameSite=None should be passed through to CDP (not stripped like Selenium)."""
        fresh_time = datetime.now(timezone.utc).isoformat()
        cookies = [
            {
                "name": "SSID", "value": "abc",
                "domain": ".google.com", "path": "/",
                "sameSite": "None",
            }
        ]
        payload = {"saved_at": fresh_time, "cookies": cookies}
        s3 = _s3_with_payload(payload)

        d = _driver()
        d.current_url = "https://www.google.com/travel/flights/saves"
        d.find_element.side_effect = NoSuchElementException()

        _try_restore_session(d, s3)

        # CDP call should include sameSite
        cdp_calls = [c for c in d.execute_cdp_cmd.call_args_list
                     if c[0][0] == "Network.setCookie"]
        assert len(cdp_calls) == 1
        assert cdp_calls[0][0][1]["sameSite"] == "None"


# ── _save_session ──────────────────────────────────────────────────────────────

class TestSaveSession:
    def test_no_op_without_bucket(self):
        original = _config.S3_BUCKET
        try:
            _config.S3_BUCKET = ""
            d = _driver()
            s3 = MagicMock()
            _save_session(d, s3)
            s3.put_object.assert_not_called()
        finally:
            _config.S3_BUCKET = original

    def test_puts_object_to_correct_bucket_and_key(self):
        d = _driver()
        d.get_cookies.return_value = [
            {"name": "SID", "value": "abc", "domain": ".google.com"}
        ]
        s3 = MagicMock()
        _save_session(d, s3)

        s3.put_object.assert_called_once()
        kwargs = s3.put_object.call_args.kwargs
        assert kwargs["Bucket"] == _config.S3_BUCKET
        assert kwargs["Key"]    == _config.SESSION_S3_KEY

    def test_saved_payload_contains_cookies(self):
        d = _driver()
        cookies = [{"name": "SID", "value": "abc", "domain": ".google.com"}]
        d.get_cookies.return_value = cookies

        s3 = MagicMock()
        _save_session(d, s3)

        body_bytes = s3.put_object.call_args.kwargs["Body"]
        payload    = json.loads(body_bytes.decode())
        assert payload["cookies"] == cookies
        assert "saved_at" in payload

    def test_handles_s3_write_error_gracefully(self):
        d = _driver()
        d.get_cookies.return_value = []
        s3 = MagicMock()
        s3.put_object.side_effect = Exception("Connection error")
        # Should not raise
        _save_session(d, s3)
