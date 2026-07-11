"""
Outbound notifications: price alerts, removal notices, health notices.

The single place where messages fan out to multiple users. Price alerts
respect each user's mute, per-route mutes, and drop threshold; rebound
(rise) alerts go only to users who opted in via /rises. Health notices
always go out (a broken tracker is something both users need to know
about).
"""
from __future__ import annotations

import logging

from gfpt.bot import format as fmt
from gfpt.bot.telegram import TelegramClient
from gfpt.bot.users import UserRegistry, route_key
from gfpt.models import PriceQuote

log = logging.getLogger(__name__)


def _route_of(meta: dict) -> str | None:
    """Outbound route key ('ORD-LAX') for per-user route mutes."""
    slices = meta.get("slices")
    first = slices[0] if isinstance(slices, list) and slices else meta
    origin, dest = first.get("origin"), first.get("destination")
    if origin and dest:
        return route_key(str(origin), str(dest))
    return None


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
        drop = (last_known_price - quote.price) if last_known_price else None
        route = _route_of(meta)
        for user in self._registry.alert_recipients():
            if route and route in self._registry.muted_routes(user.chat_id):
                continue
            # Thresholds filter drop-size only; "now tracking" is always sent.
            if (not is_new_flight and drop is not None
                    and drop < self._registry.alert_threshold(user.chat_id)):
                log.info("Drop $%d below %s's threshold — not pinged",
                         drop, user.name)
                continue
            self._client.send_message(user.chat_id, text)

    def price_rise(self, quote: PriceQuote, meta: dict,
                   low_price: int | None) -> None:
        """Rebound off the all-time low — opt-in via /rises."""
        recipients = self._registry.rise_recipients()
        if not recipients:
            return
        text = fmt.format_price_rise(quote, meta, low_price=low_price)
        route = _route_of(meta)
        for user in recipients:
            if route and route in self._registry.muted_routes(user.chat_id):
                continue
            self._client.send_message(user.chat_id, text)

    def flights_removed(self, removed: dict) -> None:
        if not removed:
            return
        text = fmt.format_removed_flights(removed)
        for user in self._registry.alert_recipients():
            self._client.send_message(user.chat_id, text)

    def failure(self, error: str, streak: int) -> bool:
        """Returns True if at least one user received the notice — the
        caller only marks it delivered (and stops retrying) on success."""
        text = fmt.format_failure_notice(error, streak)
        return any([
            self._client.send_message(user.chat_id, text)
            for user in self._registry.all_users()
        ])

    def recovery(self, streak: int) -> None:
        text = fmt.format_recovery_notice(streak)
        for user in self._registry.all_users():
            self._client.send_message(user.chat_id, text)
