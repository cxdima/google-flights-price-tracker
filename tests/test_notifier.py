"""
Unit tests for gfpt.bot.notifier — mocked Telegram client, real
UserRegistry over the dict-backed fake state.

Core product guarantee (see product-alert-philosophy): a new all-time low
pings every unmuted user by default; thresholds, route mutes, and rise
alerts are per-user opt-ins that never affect the other user.
"""
from unittest.mock import MagicMock

import pytest

from conftest import CHAT_ID_ALEX, CHAT_ID_DMITRY, FLIGHT_ID_1, SEARCH_URL_1
from gfpt.bot.notifier import Notifier
from gfpt.bot.users import UserRegistry
from gfpt.models import PriceQuote, TelegramUser

USERS = (
    TelegramUser(chat_id=CHAT_ID_DMITRY, name="Dmitry"),
    TelegramUser(chat_id=CHAT_ID_ALEX, name="Alex"),
)

META = {"origin": "ORD", "destination": "LAX", "airline": "United"}
QUOTE = PriceQuote(FLIGHT_ID_1, 450, SEARCH_URL_1)


@pytest.fixture
def client():
    c = MagicMock()
    c.send_message.return_value = True
    return c


@pytest.fixture
def registry(fake_state):
    return UserRegistry(USERS, fake_state)


def _recipients(client) -> list[str]:
    return [call[0][0] for call in client.send_message.call_args_list]


class TestPriceAlertFanOut:
    def test_new_low_pings_everyone_by_default(self, client, registry):
        Notifier(client, registry).price_alert(
            QUOTE, META, is_new_flight=False, last_known_price=500)

        assert _recipients(client) == [CHAT_ID_DMITRY, CHAT_ID_ALEX]

    def test_threshold_filters_small_drops_for_that_user_only(
        self, client, registry
    ):
        registry.set_pref(CHAT_ID_DMITRY, "threshold", 100)

        Notifier(client, registry).price_alert(
            QUOTE, META, is_new_flight=False, last_known_price=500)  # −$50

        assert _recipients(client) == [CHAT_ID_ALEX]

    def test_threshold_passes_large_drops(self, client, registry):
        registry.set_pref(CHAT_ID_DMITRY, "threshold", 50)

        Notifier(client, registry).price_alert(
            QUOTE, META, is_new_flight=False, last_known_price=500)  # −$50

        assert CHAT_ID_DMITRY in _recipients(client)

    def test_new_flight_ignores_thresholds(self, client, registry):
        registry.set_pref(CHAT_ID_DMITRY, "threshold", 100)

        Notifier(client, registry).price_alert(
            QUOTE, META, is_new_flight=True, last_known_price=None)

        assert _recipients(client) == [CHAT_ID_DMITRY, CHAT_ID_ALEX]

    def test_route_mute_silences_that_route_for_that_user_only(
        self, client, registry
    ):
        registry.set_route_muted(CHAT_ID_ALEX, "ORD", "LAX", True)

        Notifier(client, registry).price_alert(
            QUOTE, META, is_new_flight=False, last_known_price=500)

        assert _recipients(client) == [CHAT_ID_DMITRY]

    def test_route_mute_does_not_affect_other_routes(self, client, registry):
        registry.set_route_muted(CHAT_ID_ALEX, "SFO", "JFK", True)

        Notifier(client, registry).price_alert(
            QUOTE, META, is_new_flight=False, last_known_price=500)

        assert _recipients(client) == [CHAT_ID_DMITRY, CHAT_ID_ALEX]

    def test_paused_user_gets_nothing(self, client, registry):
        registry.set_muted(CHAT_ID_DMITRY, True)

        Notifier(client, registry).price_alert(
            QUOTE, META, is_new_flight=False, last_known_price=500)

        assert _recipients(client) == [CHAT_ID_ALEX]


class TestRiseAlerts:
    def test_nobody_gets_rises_by_default(self, client, registry):
        Notifier(client, registry).price_rise(QUOTE, META, low_price=400)

        client.send_message.assert_not_called()

    def test_opted_in_user_gets_the_rebound(self, client, registry):
        registry.set_pref(CHAT_ID_ALEX, "rise_alerts", True)

        Notifier(client, registry).price_rise(QUOTE, META, low_price=400)

        assert _recipients(client) == [CHAT_ID_ALEX]

    def test_route_mute_applies_to_rises_too(self, client, registry):
        registry.set_pref(CHAT_ID_ALEX, "rise_alerts", True)
        registry.set_route_muted(CHAT_ID_ALEX, "ORD", "LAX", True)

        Notifier(client, registry).price_rise(QUOTE, META, low_price=400)

        client.send_message.assert_not_called()


class TestHealthNotices:
    def test_failure_returns_true_when_any_send_succeeds(self, client, registry):
        client.send_message.side_effect = [False, True]

        assert Notifier(client, registry).failure("boom", 3) is True

    def test_failure_returns_false_when_all_sends_fail(self, client, registry):
        client.send_message.return_value = False

        assert Notifier(client, registry).failure("boom", 3) is False

    def test_failure_attempts_every_user_even_after_success(
        self, client, registry
    ):
        """any() over a generator would short-circuit and skip user 2."""
        Notifier(client, registry).failure("boom", 3)

        assert _recipients(client) == [CHAT_ID_DMITRY, CHAT_ID_ALEX]

    def test_failure_goes_to_muted_users_too(self, client, registry):
        registry.set_muted(CHAT_ID_DMITRY, True)

        Notifier(client, registry).failure("boom", 3)

        assert CHAT_ID_DMITRY in _recipients(client)
