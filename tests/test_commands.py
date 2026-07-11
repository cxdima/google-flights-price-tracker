"""
Unit tests for gfpt.bot.commands and gfpt.bot.telegram — mocked client,
real UserRegistry over the dict-backed fake state. No network.

The core guarantee under test: every reply goes ONLY to the chat that
issued the command, and strangers never get a reply at all.
"""
import json
from unittest.mock import MagicMock

import pytest

from conftest import CHAT_ID_ALEX, CHAT_ID_DMITRY
from gfpt.bot.commands import BotContext, handle_update
from gfpt.bot.telegram import TelegramClient, compute_webhook_secret
from gfpt.bot.users import UserRegistry
from gfpt.models import TelegramUser

USERS = (
    TelegramUser(chat_id=CHAT_ID_DMITRY, name="Dmitry"),
    TelegramUser(chat_id=CHAT_ID_ALEX, name="Alex"),
)


@pytest.fixture
def ctx(fake_state):
    client = MagicMock()
    client.send_message.return_value = True
    registry = UserRegistry(USERS, fake_state)
    return BotContext(client=client, registry=registry, state=fake_state)


def _update(chat_id, text, key="message"):
    return json.dumps({"update_id": 1, key: {"chat": {"id": chat_id}, "text": text}})


# ── compute_webhook_secret ─────────────────────────────────────────────────────

class TestComputeWebhookSecret:
    def test_deterministic_for_same_token(self):
        assert compute_webhook_secret("tok") == compute_webhook_secret("tok")

    def test_is_40_hex_chars(self):
        secret = compute_webhook_secret("0:test-token")

        assert len(secret) == 40
        assert all(c in "0123456789abcdef" for c in secret)

    def test_differs_for_different_tokens(self):
        assert compute_webhook_secret("token-a") != compute_webhook_secret("token-b")


# ── TelegramClient ─────────────────────────────────────────────────────────────

class TestTelegramClient:
    def test_send_message_returns_false_when_token_empty(self, monkeypatch):
        def _explode(*args, **kwargs):
            raise AssertionError("urlopen must not be called without a token")

        monkeypatch.setattr("urllib.request.urlopen", _explode)
        client = TelegramClient("")

        assert client.send_message("12345", "hello") is False


# ── handle_update: routing and auth ────────────────────────────────────────────

class TestHandleUpdateRouting:
    def test_invalid_json_returns_error(self, ctx):
        result = handle_update("not-json{", ctx)

        assert result["ok"] is False
        assert "error" in result

    def test_unauthorized_chat_gets_no_reply(self, ctx):
        result = handle_update(_update(99999999, "/status"), ctx)

        assert result.get("ignored") is True
        ctx.client.send_message.assert_not_called()

    def test_update_without_message_is_ignored(self, ctx):
        result = handle_update(json.dumps({"update_id": 7}), ctx)

        assert result.get("ignored") is True
        ctx.client.send_message.assert_not_called()

    def test_non_command_text_is_ignored(self, ctx):
        result = handle_update(_update(int(CHAT_ID_DMITRY), "hello bot"), ctx)

        assert result.get("ignored") is True
        ctx.client.send_message.assert_not_called()

    def test_edited_message_is_handled_like_message(self, ctx):
        result = handle_update(
            _update(int(CHAT_ID_DMITRY), "/status", key="edited_message"), ctx,
        )

        assert result == {"ok": True, "command": "/status"}
        ctx.client.send_message.assert_called_once()

    def test_command_with_botname_suffix_still_routes(self, ctx):
        result = handle_update(_update(int(CHAT_ID_DMITRY), "/status@MyBot"), ctx)

        assert result["command"] == "/status"
        ctx.client.send_message.assert_called_once()


# ── handle_update: per-user isolation ──────────────────────────────────────────

class TestPerUserIsolation:
    def test_status_replies_exactly_once_to_sender_only(self, ctx):
        handle_update(_update(int(CHAT_ID_DMITRY), "/status"), ctx)

        ctx.client.send_message.assert_called_once()
        assert ctx.client.send_message.call_args[0][0] == CHAT_ID_DMITRY

    def test_flights_replies_only_to_sender(self, ctx):
        handle_update(_update(int(CHAT_ID_ALEX), "/flights"), ctx)

        ctx.client.send_message.assert_called_once()
        assert ctx.client.send_message.call_args[0][0] == CHAT_ID_ALEX


# ── handle_update: individual commands ─────────────────────────────────────────

class TestCommands:
    def test_status_without_summary_mentions_no_run_data(self, ctx):
        handle_update(_update(int(CHAT_ID_DMITRY), "/status"), ctx)

        text = ctx.client.send_message.call_args[0][1]
        assert "No run data" in text
        assert "Dmitry" in text

    def test_status_with_summary_shows_run_details(self, ctx, fake_state):
        fake_state.summary = {
            "ok": True, "flights": 4, "updated": 1,
            "runtime_secs": 33.0, "finished_at": "07/08/2026 09:00",
        }

        handle_update(_update(int(CHAT_ID_DMITRY), "/status"), ctx)

        text = ctx.client.send_message.call_args[0][1]
        assert "4</b> flights tracked" in text

    def test_flights_renders_manifest(self, ctx, fake_state):
        fake_state.manifest = {}

        handle_update(_update(int(CHAT_ID_DMITRY), "/flights"), ctx)

        text = ctx.client.send_message.call_args[0][1]
        assert "No flights tracked yet" in text

    def test_pause_mutes_sender_only_and_confirms(self, ctx):
        handle_update(_update(int(CHAT_ID_DMITRY), "/pause"), ctx)

        assert ctx.registry.is_muted(CHAT_ID_DMITRY) is True
        assert ctx.registry.is_muted(CHAT_ID_ALEX) is False
        ctx.client.send_message.assert_called_once()
        chat, text = ctx.client.send_message.call_args[0][:2]
        assert chat == CHAT_ID_DMITRY
        assert "paused" in text.lower()

    def test_resume_unmutes_sender(self, ctx):
        ctx.registry.set_muted(CHAT_ID_DMITRY, True)

        handle_update(_update(int(CHAT_ID_DMITRY), "/resume"), ctx)

        assert ctx.registry.is_muted(CHAT_ID_DMITRY) is False
        assert "re-enabled" in ctx.client.send_message.call_args[0][1]

    def test_help_sends_help_text_to_sender(self, ctx):
        handle_update(_update(int(CHAT_ID_DMITRY), "/help"), ctx)

        chat, text = ctx.client.send_message.call_args[0][:2]
        assert chat == CHAT_ID_DMITRY
        assert "/status" in text
        assert "/flights" in text

    def test_unknown_command_falls_back_to_help(self, ctx):
        result = handle_update(_update(int(CHAT_ID_DMITRY), "/frobnicate"), ctx)

        assert result["command"] == "/frobnicate"
        text = ctx.client.send_message.call_args[0][1]
        assert "/help" in text or "/status" in text


class TestSettingsCommands:
    def test_threshold_set_and_confirm(self, ctx):
        handle_update(_update(int(CHAT_ID_DMITRY), "/threshold 25"), ctx)

        assert ctx.registry.alert_threshold(CHAT_ID_DMITRY) == 25
        chat, text = ctx.client.send_message.call_args[0]
        assert chat == CHAT_ID_DMITRY
        assert "$25" in text

    def test_threshold_zero_means_every_low(self, ctx):
        ctx.registry.set_pref(CHAT_ID_DMITRY, "threshold", 25)

        handle_update(_update(int(CHAT_ID_DMITRY), "/threshold 0"), ctx)

        assert ctx.registry.alert_threshold(CHAT_ID_DMITRY) == 0
        assert "every new low" in ctx.client.send_message.call_args[0][1]

    def test_threshold_garbage_shows_usage_without_change(self, ctx):
        handle_update(_update(int(CHAT_ID_DMITRY), "/threshold banana"), ctx)

        assert ctx.registry.alert_threshold(CHAT_ID_DMITRY) == 0
        assert "Usage" in ctx.client.send_message.call_args[0][1]

    def test_rises_on(self, ctx):
        handle_update(_update(int(CHAT_ID_DMITRY), "/rises on"), ctx)

        assert ctx.registry.wants_rises(CHAT_ID_DMITRY) is True

    def test_rises_off(self, ctx):
        ctx.registry.set_pref(CHAT_ID_DMITRY, "rise_alerts", True)

        handle_update(_update(int(CHAT_ID_DMITRY), "/rises off"), ctx)

        assert ctx.registry.wants_rises(CHAT_ID_DMITRY) is False

    def test_mute_route(self, ctx):
        handle_update(_update(int(CHAT_ID_DMITRY), "/mute ORD LAX"), ctx)

        assert ctx.registry.muted_routes(CHAT_ID_DMITRY) == {"ORD-LAX"}

    def test_unmute_route(self, ctx):
        ctx.registry.set_route_muted(CHAT_ID_DMITRY, "ORD", "LAX", True)

        handle_update(_update(int(CHAT_ID_DMITRY), "/unmute ord lax"), ctx)

        assert ctx.registry.muted_routes(CHAT_ID_DMITRY) == set()

    def test_mute_without_args_lists_routes(self, ctx):
        ctx.registry.set_route_muted(CHAT_ID_DMITRY, "ORD", "LAX", True)

        handle_update(_update(int(CHAT_ID_DMITRY), "/mute"), ctx)

        text = ctx.client.send_message.call_args[0][1]
        assert "ORD → LAX" in text
        assert ctx.registry.muted_routes(CHAT_ID_DMITRY) == {"ORD-LAX"}  # unchanged

    def test_settings_shows_current_state(self, ctx):
        ctx.registry.set_pref(CHAT_ID_DMITRY, "threshold", 10)
        ctx.registry.set_pref(CHAT_ID_DMITRY, "rise_alerts", True)

        handle_update(_update(int(CHAT_ID_DMITRY), "/settings"), ctx)

        chat, text = ctx.client.send_message.call_args[0]
        assert chat == CHAT_ID_DMITRY
        assert "$10" in text
        assert "on" in text

    def test_settings_replies_only_to_sender(self, ctx):
        handle_update(_update(int(CHAT_ID_ALEX), "/settings"), ctx)

        for call in ctx.client.send_message.call_args_list:
            assert call[0][0] == CHAT_ID_ALEX

    def test_status_surfaces_failure_streak(self, ctx):
        ctx.state.summary = {"ok": False, "flights": 0, "error": "boom",
                             "finished_at": "07/09/2026 10:00"}
        ctx.state.health = {"consecutive_failures": 4}

        handle_update(_update(int(CHAT_ID_DMITRY), "/status"), ctx)

        text = ctx.client.send_message.call_args[0][1]
        assert "4" in text and "consecutive failed" in text
