"""
Telegram command handling.

Every reply goes ONLY to the chat that issued the command — commands are
private to each user. The only broadcasts in the system are price alerts
and failure notices, and those go through Notifier, never through here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from gfpt.bot import format as fmt
from gfpt.bot.telegram import TelegramClient
from gfpt.bot.users import UserRegistry
from gfpt.models import RunSummary
from gfpt.storage.state import StateStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotContext:
    """Everything command handlers need, injected for testability."""
    client: TelegramClient
    registry: UserRegistry
    state: StateStore


def handle_update(body: str, ctx: BotContext) -> dict:
    """
    Process one Telegram webhook update.

    Returns a small result dict (logged, and echoed in the HTTP response).
    Unknown chats are ignored silently — replying would confirm the bot
    exists to whoever is probing it.
    """
    try:
        update = json.loads(body)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid JSON"}

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    user = ctx.registry.get(chat_id)
    if user is None:
        log.info("Ignoring update from unauthorized chat %s", chat_id or "<none>")
        return {"ok": True, "ignored": True}
    if not text.startswith("/"):
        return {"ok": True, "ignored": True}

    command = text.split()[0].split("@")[0].lower()
    log.info("Command %s from %s (%s)", command, user.name, chat_id)

    if command == "/status":
        _reply_status(ctx, user)
    elif command == "/flights":
        ctx.client.send_message(chat_id, fmt.format_flights(ctx.state.load_manifest()))
    elif command == "/pause":
        ctx.registry.set_muted(chat_id, True)
        ctx.client.send_message(
            chat_id,
            "🔕 Alerts paused for you. Others are unaffected. /resume to re-enable.",
        )
    elif command == "/resume":
        ctx.registry.set_muted(chat_id, False)
        ctx.client.send_message(chat_id, "🔔 Alerts re-enabled for you.")
    else:
        # /help and anything unrecognized
        ctx.client.send_message(chat_id, fmt.format_help(user))

    return {"ok": True, "command": command}


def _reply_status(ctx: BotContext, user) -> None:
    summary = RunSummary.from_dict(ctx.state.load_summary())
    muted = ctx.registry.is_muted(user.chat_id)
    ctx.client.send_message(
        user.chat_id, fmt.format_status(summary, user, muted=muted)
    )
