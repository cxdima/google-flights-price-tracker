"""
Unit tests for gfpt.handler — the Lambda entry point's event routing.

The two security-critical properties pinned down here:
  1. Webhook requests without Telegram's secret token get a 403 and can
     never reach the bot (or the tracker).
  2. Unrecognized events are rejected instead of falling through to a
     full tracker run (the old handler let anyone with the public URL
     trigger 2 GB x 60 s invocations).
"""
import base64
import json
import os
from unittest.mock import MagicMock

import pytest

import gfpt.handler as handler_mod
from gfpt.bot.telegram import compute_webhook_secret

SECRET_HEADER = "x-telegram-bot-api-secret-token"


def _correct_secret() -> str:
    return compute_webhook_secret(os.environ["TELEGRAM_BOT_TOKEN"])


def _webhook_event(body="{}", headers=None, is_base64=False) -> dict:
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": headers or {},
        "body": body,
        "isBase64Encoded": is_base64,
    }


@pytest.fixture
def forbid_tracker(monkeypatch):
    """Fail the test if anything tries to start a tracker run."""
    def _explode(*args, **kwargs):
        raise AssertionError("tracker run must not be triggered by this event")

    monkeypatch.setattr(handler_mod, "run_and_report", _explode)
    monkeypatch.setattr(handler_mod, "_build_tracker_deps", _explode)


@pytest.fixture
def bot_wired(monkeypatch):
    """Replace bot wiring and capture the body passed to handle_update."""
    seen = {}

    def _fake_handle_update(body, ctx):
        seen["body"] = body
        seen["ctx"] = ctx
        return {"ok": True, "command": "/status"}

    monkeypatch.setattr(handler_mod, "_build_bot_context", lambda s: MagicMock())
    monkeypatch.setattr(handler_mod, "handle_update", _fake_handle_update)
    return seen


# ── Scheduled events ───────────────────────────────────────────────────────────

class TestScheduledEvents:
    @pytest.mark.parametrize("source", ["eventbridge", "aws.events", "deploy-test"])
    def test_scheduled_source_runs_tracker(self, monkeypatch, source):
        deps = object()
        monkeypatch.setattr(handler_mod, "_build_tracker_deps", lambda: deps)
        monkeypatch.setattr(
            handler_mod, "run_and_report",
            lambda d: {"ok": True, "ran_with": d is deps},
        )

        result = handler_mod.handler({"source": source}, None)

        assert result == {"ok": True, "ran_with": True}


# ── Unrecognized events (security fix) ─────────────────────────────────────────

class TestUnrecognizedEvents:
    def test_empty_event_rejected_and_tracker_not_run_security_fix(
        self, forbid_tracker
    ):
        """The old handler fell through to a full tracker run for ANY
        unknown payload — anyone with the public URL could burn Lambda
        time at will. That path must stay dead."""
        result = handler_mod.handler({}, None)

        assert result == {"ok": False, "error": "unrecognized event"}

    def test_random_dict_rejected_without_tracker_run(self, forbid_tracker):
        result = handler_mod.handler({"detail": "curl-probe", "foo": 1}, None)

        assert result == {"ok": False, "error": "unrecognized event"}

    def test_unknown_source_value_rejected(self, forbid_tracker):
        result = handler_mod.handler({"source": "not-a-real-source"}, None)

        assert result == {"ok": False, "error": "unrecognized event"}

    def test_non_dict_event_rejected(self, forbid_tracker):
        result = handler_mod.handler("just a string", None)

        assert result == {"ok": False, "error": "unrecognized event"}


# ── Webhook: secret token gate ─────────────────────────────────────────────────

class TestWebhookAuth:
    def test_missing_secret_header_returns_403(self, forbid_tracker, monkeypatch):
        def _explode(*args, **kwargs):
            raise AssertionError("handle_update must not run without the secret")

        monkeypatch.setattr(handler_mod, "handle_update", _explode)

        result = handler_mod.handler(_webhook_event(), None)

        assert result["statusCode"] == 403

    def test_wrong_secret_returns_403(self, forbid_tracker, monkeypatch):
        def _explode(*args, **kwargs):
            raise AssertionError("handle_update must not run with a bad secret")

        monkeypatch.setattr(handler_mod, "handle_update", _explode)
        event = _webhook_event(headers={SECRET_HEADER: "wrong-secret"})

        result = handler_mod.handler(event, None)

        assert result["statusCode"] == 403

    def test_correct_secret_returns_200(self, forbid_tracker, bot_wired):
        event = _webhook_event(
            body='{"update_id": 1}',
            headers={SECRET_HEADER: _correct_secret()},
        )

        result = handler_mod.handler(event, None)

        assert result["statusCode"] == 200
        assert json.loads(result["body"]) == {"ok": True, "command": "/status"}

    def test_secret_header_lookup_is_case_insensitive(
        self, forbid_tracker, bot_wired
    ):
        # Telegram may send the header in canonical casing
        event = _webhook_event(
            body='{"update_id": 1}',
            headers={"X-Telegram-Bot-Api-Secret-Token": _correct_secret()},
        )

        result = handler_mod.handler(event, None)

        assert result["statusCode"] == 200

    def test_empty_bot_token_rejects_even_matching_derived_secret(
        self, forbid_tracker, monkeypatch
    ):
        """With no bot token configured, compute_webhook_secret('') is a
        publicly computable constant — the webhook must reject outright
        rather than accept it."""
        from gfpt.config import load_settings

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        load_settings.cache_clear()

        def _explode(*args, **kwargs):
            raise AssertionError("handle_update must not run without a bot token")

        monkeypatch.setattr(handler_mod, "handle_update", _explode)
        event = _webhook_event(
            body='{"update_id": 1}',
            headers={SECRET_HEADER: compute_webhook_secret("")},
        )

        result = handler_mod.handler(event, None)

        assert result["statusCode"] == 403


# ── Webhook: body handling ─────────────────────────────────────────────────────

class TestWebhookBody:
    def test_plain_body_passed_through_to_bot(self, forbid_tracker, bot_wired):
        body = '{"update_id": 42}'
        event = _webhook_event(body=body, headers={SECRET_HEADER: _correct_secret()})

        handler_mod.handler(event, None)

        assert bot_wired["body"] == body

    def test_base64_body_is_decoded(self, forbid_tracker, bot_wired):
        body = json.dumps({"update_id": 42, "message": {"text": "/status"}})
        encoded = base64.b64encode(body.encode()).decode()
        event = _webhook_event(
            body=encoded,
            headers={SECRET_HEADER: _correct_secret()},
            is_base64=True,
        )

        handler_mod.handler(event, None)

        assert bot_wired["body"] == body

    def test_missing_body_defaults_to_empty_string(self, forbid_tracker, bot_wired):
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "headers": {SECRET_HEADER: _correct_secret()},
        }

        result = handler_mod.handler(event, None)

        assert result["statusCode"] == 200
        assert bot_wired["body"] == ""
