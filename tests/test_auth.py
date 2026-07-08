"""
Unit tests for gfpt.tracking.auth — mocked driver and StateStore, no
browser, no AWS, no network.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from selenium.common.exceptions import NoSuchElementException

from gfpt.config import FLIGHTS_SAVES_URL
from gfpt.tracking.auth import _try_restore_session, is_authenticated, save_session


def _driver(url="https://www.google.com/travel/flights/saves"):
    """Minimal mock Selenium driver: logged in, no sign-in link."""
    d = MagicMock()
    d.current_url = url
    d.find_element.side_effect = NoSuchElementException()
    return d


def _fresh_session(cookies):
    return {
        "saved_at": datetime.now(UTC).isoformat(),
        "cookies": cookies,
    }


def _state_with_session(payload):
    state = MagicMock()
    state.load_session.return_value = payload
    return state


def _set_cookie_calls(driver):
    return [
        c for c in driver.execute_cdp_cmd.call_args_list
        if c[0][0] == "Network.setCookie"
    ]


# ── is_authenticated ───────────────────────────────────────────────────────────

class TestIsAuthenticated:
    def test_false_on_accounts_google(self):
        d = _driver("https://accounts.google.com/signin/v2/identifier")

        assert is_authenticated(d) is False

    def test_false_on_myaccount_google(self):
        d = _driver("https://myaccount.google.com/intro/passkey")

        assert is_authenticated(d) is False

    def test_false_when_signin_link_visible(self):
        d = _driver()
        d.find_element.side_effect = None
        d.find_element.return_value = MagicMock()  # ServiceLogin link found

        assert is_authenticated(d) is False

    def test_true_on_saves_page_without_signin_link(self):
        d = _driver()  # find_element raises NoSuchElementException

        assert is_authenticated(d) is True

    def test_true_on_any_google_non_accounts_url(self):
        d = _driver("https://www.google.com/travel/flights")

        assert is_authenticated(d) is True


# ── _try_restore_session ───────────────────────────────────────────────────────

class TestTryRestoreSession:
    def test_returns_false_when_no_session_in_state(self):
        d = _driver()
        state = _state_with_session(None)

        assert _try_restore_session(d, state) is False
        d.execute_cdp_cmd.assert_not_called()

    def test_returns_false_for_expired_session(self):
        old_time = (datetime.now(UTC) - timedelta(hours=10)).isoformat()
        state = _state_with_session({"saved_at": old_time, "cookies": []})
        d = _driver()

        assert _try_restore_session(d, state) is False
        d.get.assert_not_called()

    def test_fresh_session_injects_cookies_via_cdp(self):
        cookies = [{"name": "SID", "value": "fake-sid",
                    "domain": ".google.com", "path": "/"}]
        state = _state_with_session(_fresh_session(cookies))
        d = _driver()

        result = _try_restore_session(d, state)

        assert result is True
        calls = _set_cookie_calls(d)
        assert len(calls) == 1
        injected = calls[0][0][1]
        assert injected["name"] == "SID"
        assert injected["value"] == "fake-sid"
        assert injected["domain"] == ".google.com"

    def test_fresh_session_navigates_to_saves_page(self):
        state = _state_with_session(_fresh_session([]))
        d = _driver()

        _try_restore_session(d, state)

        d.get.assert_called_once_with(FLIGHTS_SAVES_URL)

    def test_returns_false_when_google_requires_reauth(self):
        state = _state_with_session(_fresh_session([]))
        d = _driver("https://accounts.google.com/signin")  # redirected back

        assert _try_restore_session(d, state) is False

    def test_returns_false_when_navigation_raises(self):
        state = _state_with_session(_fresh_session([]))
        d = _driver()
        d.get.side_effect = Exception("net::ERR_TIMED_OUT")

        assert _try_restore_session(d, state) is False

    def test_same_site_none_passes_through_to_cdp(self):
        cookies = [{"name": "SSID", "value": "abc", "domain": ".google.com",
                    "path": "/", "sameSite": "None"}]
        state = _state_with_session(_fresh_session(cookies))
        d = _driver()

        _try_restore_session(d, state)

        assert _set_cookie_calls(d)[0][0][1]["sameSite"] == "None"

    def test_same_site_lax_and_strict_pass_through(self):
        cookies = [
            {"name": "A", "value": "1", "sameSite": "Lax"},
            {"name": "B", "value": "2", "sameSite": "Strict"},
        ]
        state = _state_with_session(_fresh_session(cookies))
        d = _driver()

        _try_restore_session(d, state)

        injected = [c[0][1] for c in _set_cookie_calls(d)]
        assert injected[0]["sameSite"] == "Lax"
        assert injected[1]["sameSite"] == "Strict"

    def test_invalid_same_site_value_is_dropped(self):
        cookies = [{"name": "C", "value": "3", "sameSite": "no_restriction"}]
        state = _state_with_session(_fresh_session(cookies))
        d = _driver()

        _try_restore_session(d, state)

        assert "sameSite" not in _set_cookie_calls(d)[0][0][1]

    def test_cookie_expiry_mapped_to_cdp_expires(self):
        cookies = [{"name": "D", "value": "4", "expiry": 1999999999.7}]
        state = _state_with_session(_fresh_session(cookies))
        d = _driver()

        _try_restore_session(d, state)

        assert _set_cookie_calls(d)[0][0][1]["expires"] == 1999999999


# ── save_session ───────────────────────────────────────────────────────────────

class TestSaveSession:
    def test_saves_driver_cookies_to_state(self):
        cookies = [{"name": "SID", "value": "abc", "domain": ".google.com"}]
        d = _driver()
        d.get_cookies.return_value = cookies
        state = MagicMock()
        state.save_session.return_value = True

        save_session(d, state)

        state.save_session.assert_called_once()
        payload = state.save_session.call_args[0][0]
        assert payload["cookies"] == cookies

    def test_saved_at_is_parseable_iso_timestamp(self):
        d = _driver()
        d.get_cookies.return_value = []
        state = MagicMock()

        save_session(d, state)

        payload = state.save_session.call_args[0][0]
        parsed = datetime.fromisoformat(payload["saved_at"])
        assert parsed.tzinfo is not None

    def test_does_not_raise_when_state_write_fails(self):
        d = _driver()
        d.get_cookies.return_value = []
        state = MagicMock()
        state.save_session.return_value = False

        save_session(d, state)  # must not raise
