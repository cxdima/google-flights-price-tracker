"""
Outbound notifications: price alerts, removal notices, health notices.

The single place where messages fan out to multiple users. Respects each
user's mute preference for price alerts; health notices always go out
(a broken tracker is something both users need to know about).
"""
from __future__ import annotations

import logging

from gfpt.bot import format as fmt
from gfpt.bot.telegram import TelegramClient
from gfpt.bot.users import UserRegistry
from gfpt.models import PriceQuote

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, client: TelegramClient, registry: UserRegistry):
        self._client = client
        self._registry = registry

    def price_alert(self, quote: PriceQuote, meta: dict,
                    is_new_flight: bool, last_known_price: int | None) -> None:
        text = fmt.format_price_alert(
            quote, meta,
            is_new_flight=is_new_flight,
            last_known_price=last_known_price,
        )
        for user in self._registry.alert_recipients():
            self._client.send_message(user.chat_id, text)

    def flights_removed(self, removed: dict) -> None:
        if not removed:
            return
        text = fmt.format_removed_flights(removed)
        for user in self._registry.alert_recipients():
            self._client.send_message(user.chat_id, text)

    def failure(self, error: str, streak: int) -> None:
        text = fmt.format_failure_notice(error, streak)
        for user in self._registry.all_users():
            self._client.send_message(user.chat_id, text)

    def recovery(self, streak: int) -> None:
        text = fmt.format_recovery_notice(streak)
        for user in self._registry.all_users():
            self._client.send_message(user.chat_id, text)
