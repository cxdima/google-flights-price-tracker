"""Unit tests for gfpt.bot.users.UserRegistry — dict-backed fake state."""
from conftest import CHAT_ID_ALEX, CHAT_ID_DMITRY
from gfpt.bot.users import UserRegistry
from gfpt.models import TelegramUser

USERS = (
    TelegramUser(chat_id=CHAT_ID_DMITRY, name="Dmitry"),
    TelegramUser(chat_id=CHAT_ID_ALEX, name="Alex"),
)


def _registry(fake_state):
    return UserRegistry(USERS, fake_state)


class TestLookup:
    def test_get_returns_user_by_string_id(self, fake_state):
        user = _registry(fake_state).get(CHAT_ID_DMITRY)

        assert user == USERS[0]

    def test_get_accepts_int_chat_id(self, fake_state):
        assert _registry(fake_state).get(12345) == USERS[0]

    def test_get_unknown_chat_returns_none(self, fake_state):
        assert _registry(fake_state).get("99999") is None

    def test_all_users_returns_everyone(self, fake_state):
        assert _registry(fake_state).all_users() == USERS


class TestMutePreferences:
    def test_users_are_unmuted_by_default(self, fake_state):
        assert _registry(fake_state).is_muted(CHAT_ID_DMITRY) is False

    def test_set_muted_round_trips_through_state(self, fake_state):
        registry = _registry(fake_state)

        registry.set_muted(CHAT_ID_DMITRY, True)

        assert registry.is_muted(CHAT_ID_DMITRY) is True
        assert fake_state.prefs[CHAT_ID_DMITRY]["muted"] is True

    def test_unmute_round_trips_through_state(self, fake_state):
        registry = _registry(fake_state)
        registry.set_muted(CHAT_ID_DMITRY, True)

        registry.set_muted(CHAT_ID_DMITRY, False)

        assert registry.is_muted(CHAT_ID_DMITRY) is False

    def test_set_muted_accepts_int_chat_id(self, fake_state):
        registry = _registry(fake_state)

        registry.set_muted(12345, True)

        assert registry.is_muted("12345") is True

    def test_set_muted_does_not_clobber_other_users_prefs(self, fake_state):
        fake_state.prefs = {CHAT_ID_ALEX: {"muted": True, "digest": "daily"}}
        registry = _registry(fake_state)

        registry.set_muted(CHAT_ID_DMITRY, True)

        assert fake_state.prefs[CHAT_ID_ALEX] == {"muted": True, "digest": "daily"}

    def test_set_muted_preserves_own_other_prefs(self, fake_state):
        fake_state.prefs = {CHAT_ID_DMITRY: {"digest": "weekly"}}
        registry = _registry(fake_state)

        registry.set_muted(CHAT_ID_DMITRY, True)

        assert fake_state.prefs[CHAT_ID_DMITRY] == {"digest": "weekly", "muted": True}


class TestAlertRecipients:
    def test_all_users_receive_alerts_by_default(self, fake_state):
        assert _registry(fake_state).alert_recipients() == USERS

    def test_muted_user_is_excluded(self, fake_state):
        registry = _registry(fake_state)
        registry.set_muted(CHAT_ID_DMITRY, True)

        recipients = registry.alert_recipients()

        assert [u.chat_id for u in recipients] == [CHAT_ID_ALEX]

    def test_resumed_user_is_included_again(self, fake_state):
        registry = _registry(fake_state)
        registry.set_muted(CHAT_ID_DMITRY, True)
        registry.set_muted(CHAT_ID_DMITRY, False)

        assert registry.alert_recipients() == USERS
