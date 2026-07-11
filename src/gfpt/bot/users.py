"""
Authorized users and their preferences.

The user list itself is static configuration (TELEGRAM_USERS env var) —
today that's the two of us, later it can move to a table without touching
callers. Mutable per-user state lives in the S3 state store:

  muted         bool  — /pause: no price/removal alerts at all
  threshold     int   — /threshold: minimum $ drop to be pinged (0 = every
                        new low, the default — the product's core signal)
  rise_alerts   bool  — /rises: opt-in "price rebounded off its low" pings
  muted_routes  list  — /mute ORD LAX: routes this user never hears about
"""
from __future__ import annotations

import logging

from gfpt.models import TelegramUser
from gfpt.storage.state import StateStore

log = logging.getLogger(__name__)


def route_key(origin: str, destination: str) -> str:
    return f"{origin.strip().upper()}-{destination.strip().upper()}"


class UserRegistry:
    def __init__(self, users: tuple[TelegramUser, ...], state: StateStore):
        self._users = {u.chat_id: u for u in users}
        self._state = state

    def get(self, chat_id: str) -> TelegramUser | None:
        return self._users.get(str(chat_id))

    def all_users(self) -> tuple[TelegramUser, ...]:
        return tuple(self._users.values())

    # ── Preferences ────────────────────────────────────────────────────────────

    def get_prefs(self, chat_id: str) -> dict:
        return self._state.load_user_prefs().get(str(chat_id), {})

    def set_pref(self, chat_id: str, key: str, value) -> None:
        chat_id = str(chat_id)
        prefs = self._state.load_user_prefs()
        updated = {**prefs, chat_id: {**prefs.get(chat_id, {}), key: value}}
        self._state.save_user_prefs(updated)

    def is_muted(self, chat_id: str) -> bool:
        return bool(self.get_prefs(chat_id).get("muted"))

    def set_muted(self, chat_id: str, muted: bool) -> None:
        self.set_pref(chat_id, "muted", muted)

    def alert_threshold(self, chat_id: str) -> int:
        """Minimum price drop (in dollars) this user wants to be pinged
        about. 0 (default) = every new low."""
        try:
            return max(0, int(self.get_prefs(chat_id).get("threshold", 0)))
        except (TypeError, ValueError):
            return 0

    def wants_rises(self, chat_id: str) -> bool:
        return bool(self.get_prefs(chat_id).get("rise_alerts"))

    def muted_routes(self, chat_id: str) -> set[str]:
        routes = self.get_prefs(chat_id).get("muted_routes")
        if not isinstance(routes, list):
            return set()
        return {str(r).upper() for r in routes}

    def set_route_muted(self, chat_id: str, origin: str, destination: str,
                        muted: bool) -> set[str]:
        """Mute/unmute one route for one user; returns the updated set."""
        routes = self.muted_routes(chat_id)
        key = route_key(origin, destination)
        routes = routes | {key} if muted else routes - {key}
        self.set_pref(chat_id, "muted_routes", sorted(routes))
        return routes

    # ── Alert fan-out filters ──────────────────────────────────────────────────

    def alert_recipients(self) -> tuple[TelegramUser, ...]:
        """Users who should receive price alerts (authorized and not muted)."""
        prefs = self._state.load_user_prefs()
        return tuple(
            u for u in self._users.values()
            if not prefs.get(u.chat_id, {}).get("muted")
        )

    def rise_recipients(self) -> tuple[TelegramUser, ...]:
        """Unmuted users who opted into rise/rebound alerts."""
        prefs = self._state.load_user_prefs()
        return tuple(
            u for u in self._users.values()
            if not prefs.get(u.chat_id, {}).get("muted")
            and prefs.get(u.chat_id, {}).get("rise_alerts")
        )
