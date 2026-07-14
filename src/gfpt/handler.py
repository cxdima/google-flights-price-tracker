"""
AWS Lambda entry point — strict event routing.

Two legitimate callers, each with its own gate:

  EventBridge schedule  →  runs the tracker.
      Matched on the explicit source field of the rule's input.

  Telegram webhook (public Function URL)  →  bot commands ONLY.
      Every request must carry the secret token Telegram was configured
      with (X-Telegram-Bot-Api-Secret-Token). Anything else gets a 403.

The previous version fell through to a full tracker run for ANY payload it
didn't recognize — meaning anyone who found the public URL could trigger
2 GB × 60 s Lambda invocations at will. That path no longer exists.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
from functools import lru_cache

from gfpt.bot.commands import BotContext, handle_update
from gfpt.bot.notifier import Notifier
from gfpt.bot.telegram import TelegramClient, compute_webhook_secret
from gfpt.bot.users import UserRegistry
from gfpt.config import Settings, load_settings
from gfpt.storage.dynamo import PriceHistory
from gfpt.storage.state import StateStore
from gfpt.tracking.runner import TrackerDeps, run_and_report

# force=True: the Lambda runtime pre-installs a root handler, which makes a
# plain basicConfig() a silent no-op — INFO lines (GFPT_SUMMARY, capture and
# re-fire diagnostics) never reached CloudWatch without it.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    force=True,
)
log = logging.getLogger(__name__)

_SCHEDULED_SOURCES = {"eventbridge", "aws.events", "deploy-test"}


def handler(event, context):
    if _is_http_request(event):
        return _handle_webhook(event)
    if isinstance(event, dict) and event.get("source") in _SCHEDULED_SOURCES:
        return run_and_report(_build_tracker_deps())

    log.warning("Unrecognized event — ignoring: %s", str(event)[:200])
    return {"ok": False, "error": "unrecognized event"}


# ── Telegram webhook ───────────────────────────────────────────────────────────

def _is_http_request(event) -> bool:
    if not isinstance(event, dict):
        return False
    request_context = event.get("requestContext")
    return isinstance(request_context, dict) and "http" in request_context


def _handle_webhook(event: dict) -> dict:
    settings = load_settings()
    # Without a bot token there is no legitimate webhook traffic — and the
    # secret derived from an empty token would be a publicly computable value.
    if not settings.telegram_bot_token:
        log.warning("Webhook rejected: TELEGRAM_BOT_TOKEN not configured")
        return {"statusCode": 403, "body": "forbidden"}

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    provided = headers.get("x-telegram-bot-api-secret-token", "")
    expected = compute_webhook_secret(settings.telegram_bot_token)
    if not hmac.compare_digest(provided, expected):
        log.warning("Webhook rejected: bad or missing secret token")
        return {"statusCode": 403, "body": "forbidden"}

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")

    result = handle_update(body, _build_bot_context(settings))
    return {"statusCode": 200, "body": json.dumps(result)}


# ── Dependency wiring ──────────────────────────────────────────────────────────
# Cached so warm Lambda containers reuse boto3 clients across invocations.

@lru_cache(maxsize=1)
def _build_bot_context(settings: Settings) -> BotContext:
    state = StateStore(settings.s3_bucket, region=settings.aws_region)
    client = TelegramClient(settings.telegram_bot_token)
    registry = UserRegistry(settings.telegram_users, state)
    return BotContext(client=client, registry=registry, state=state)


@lru_cache(maxsize=1)
def _build_tracker_deps() -> TrackerDeps:
    settings = load_settings()
    state = StateStore(settings.s3_bucket, region=settings.aws_region)
    client = TelegramClient(settings.telegram_bot_token)
    registry = UserRegistry(settings.telegram_users, state)
    return TrackerDeps(
        settings=settings,
        state=state,
        history=PriceHistory(settings.dynamodb_table, region=settings.aws_region),
        notifier=Notifier(client, registry),
    )
