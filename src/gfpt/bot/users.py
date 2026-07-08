"""
Authorized users and their preferences.

The user list itself is static configuration (TELEGRAM_USERS env var) —
today that's the two of us, later it can move to a table without touching
callers. Mutable per-user state (muted) lives in the S3 state store.
"""
from __future__ import annotations

import logging

from gfpt.models import TelegramUser
from gfpt.storage.state import StateStore

log = logging.getLogger(__name__)


class UserRegistry:
    def __init__(self, users: tuple[TelegramUser, ...], state: StateStore):
        self._users = {u.chat_id: u for u in users}
        self._state = state

    def get(self, chat_id: str) -> TelegramUser | None:
        return self._users.get(str(chat_id))

    def all_users(self) -> tuple[TelegramUser, ...]:
        return tuple(self._users.values())

    # ── Preferences ────────────────────────────────────────────────────────────

    def is_muted(self, chat_id: str) -> bool:
        prefs = self._state.load_user_prefs()
        return bool(prefs.get(str(chat_id), {}).get("muted"))

    def set_muted(self, chat_id: str, muted: bool) -> None:
        chat_id = str(chat_id)
        prefs = self._state.load_user_prefs()
        updated = {**prefs, chat_id: {**prefs.get(chat_id, {}), "muted": muted}}
        self._state.save_user_prefs(updated)

    def alert_recipients(self) -> tuple[TelegramUser, ...]:
        """Users who should receive price alerts (authorized and not muted)."""
        prefs = self._state.load_user_prefs()
        return tuple(
            u for u in self._users.values()
            if not prefs.get(u.chat_id, {}).get("muted")
        )
