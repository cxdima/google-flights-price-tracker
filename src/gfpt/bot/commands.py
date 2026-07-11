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

    parts = text.split()
    command = parts[0].split("@")[0].lower()
    args = parts[1:]
    log.info("Command %s from %s (%s)", command, user.name, chat_id)

    if command == "/status":
        _reply_status(ctx, user)
    elif command == "/flights":
        ctx.client.send_message(chat_id, fmt.format_flights(ctx.state.load_manifest()))
    elif command == "/settings":
        _reply_settings(ctx, user)
    elif command == "/threshold":
        _handle_threshold(ctx, user, args)
    elif command == "/rises":
        _handle_rises(ctx, user, args)
    elif command in ("/mute", "/unmute"):
        _handle_route_mute(ctx, user, args, mute=(command == "/mute"))
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
    streak = ctx.state.load_failure_streak()
    ctx.client.send_message(
        user.chat_id,
        fmt.format_status(summary, user, muted=muted, failure_streak=streak),
    )


def _reply_settings(ctx: BotContext, user) -> None:
    ctx.client.send_message(
        user.chat_id,
        fmt.format_settings(
            user,
            muted=ctx.registry.is_muted(user.chat_id),
            threshold=ctx.registry.alert_threshold(user.chat_id),
            rises=ctx.registry.wants_rises(user.chat_id),
            muted_routes=ctx.registry.muted_routes(user.chat_id),
        ),
    )


def _handle_threshold(ctx: BotContext, user, args: list[str]) -> None:
    """/threshold        → show current
       /threshold 25     → only ping for drops ≥ $25
       /threshold 0|off  → every new low (the default)"""
    if not args:
        current = ctx.registry.alert_threshold(user.chat_id)
        ctx.client.send_message(
            user.chat_id,
            f"📉 Your drop threshold: "
            f"<b>{f'${current:,}' if current else 'every new low'}</b>.\n"
            "<i>/threshold 25 — only drops of $25+. /threshold 0 — every low.</i>",
        )
        return
    raw = args[0].lstrip("$")
    if raw.lower() in ("off", "none"):
        raw = "0"
    try:
        value = max(0, int(raw))
    except ValueError:
        ctx.client.send_message(
            user.chat_id, "Usage: <b>/threshold 25</b> (or 0 for every new low)."
        )
        return
    ctx.registry.set_pref(user.chat_id, "threshold", value)
    ctx.client.send_message(
        user.chat_id,
        f"📉 You'll be pinged for drops of <b>${value:,}+</b>."
        if value else
        "📉 You'll be pinged on <b>every new low</b> (the default).",
    )


def _handle_rises(ctx: BotContext, user, args: list[str]) -> None:
    """/rises on|off — opt into rebound-off-the-low pings."""
    arg = args[0].lower() if args else ""
    if arg not in ("on", "off"):
        current = ctx.registry.wants_rises(user.chat_id)
        ctx.client.send_message(
            user.chat_id,
            f"📈 Rebound alerts are <b>{'on' if current else 'off'}</b> for you.\n"
            "<i>/rises on — get pinged when a price climbs off its low.</i>",
        )
        return
    ctx.registry.set_pref(user.chat_id, "rise_alerts", arg == "on")
    ctx.client.send_message(
        user.chat_id,
        "📈 Rebound alerts <b>on</b> — you'll know when a low starts slipping away."
        if arg == "on" else
        "📈 Rebound alerts <b>off</b>.",
    )


def _handle_route_mute(ctx: BotContext, user, args: list[str], mute: bool) -> None:
    """/mute ORD LAX — silence one route for this user only."""
    if len(args) != 2 or not all(a.isalpha() and 2 <= len(a) <= 4 for a in args):
        muted_routes = ctx.registry.muted_routes(user.chat_id)
        routes = (", ".join(sorted(r.replace("-", " → ") for r in muted_routes))
                  if muted_routes else "none")
        ctx.client.send_message(
            user.chat_id,
            f"🔇 Your muted routes: <b>{routes}</b>\n"
            f"<i>Usage: <b>/{'mute' if mute else 'unmute'} ORD LAX</b> "
            "(origin and destination airport codes).</i>",
        )
        return
    origin, dest = args[0].upper(), args[1].upper()
    ctx.registry.set_route_muted(user.chat_id, origin, dest, mute)
    ctx.client.send_message(
        user.chat_id,
        f"🔇 <b>{origin} → {dest}</b> muted for you. /unmute {origin} {dest} to undo."
        if mute else
        f"🔔 <b>{origin} → {dest}</b> unmuted for you.",
    )
