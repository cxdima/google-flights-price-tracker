"""Thin Telegram Bot API client (stdlib only — no external HTTP deps)."""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org"


def compute_webhook_secret(bot_token: str) -> str:
    """
    Deterministic webhook secret derived from the bot token.

    Telegram echoes this back in the X-Telegram-Bot-Api-Secret-Token header
    on every webhook call, letting us reject any other traffic to the public
    Lambda Function URL. Deriving it from the token means the deploy script
    and the Lambda agree without managing an extra secret.
    """
    return hashlib.sha256(f"gfpt-webhook:{bot_token}".encode()).hexdigest()[:40]


class TelegramClient:
    def __init__(self, bot_token: str):
        self._token = bot_token

    def send_message(self, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
        """POST a message to a single chat. Returns True on success."""
        if not self._token:
            log.warning("TELEGRAM_BOT_TOKEN not set — dropping message")
            return False

        url = f"{_TG_API}/bot{self._token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            if result.get("ok"):
                return True
            log.warning("Telegram API error for chat %s: %s",
                        chat_id, result.get("description"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            log.warning("Telegram HTTP %s for chat %s: %s", exc.code, chat_id, body[:200])
        except Exception as exc:
            log.error("Telegram send failed for chat %s: %s", chat_id, exc)
        return False
