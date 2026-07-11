"""
Telegram message builders (HTML parse mode).

Pure functions: data in, string out — no I/O, fully unit-testable.
Metadata dicts follow the manifest shape documented in storage/state.py;
any field may be missing, and every builder must degrade gracefully
(show what we know, never "???").
"""
from __future__ import annotations

from datetime import date, datetime

from gfpt.config import TZ
from gfpt.models import PriceQuote, RunSummary, TelegramUser


def _h(text) -> str:
    """Escape text for Telegram HTML mode (quote included — scraped URLs
    land inside href="..." attributes, and one stray quote would make the
    Telegram API reject the whole message)."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt_date(date_str: str) -> str:
    """Convert '2026-05-08' → 'May 8'."""
    try:
        parsed = date.fromisoformat(date_str)
        return f"{parsed.strftime('%b')} {parsed.day}"
    except (ValueError, TypeError):
        return date_str


def _flight_label(meta: dict) -> str:
    """'WN 3492', 'WN 2164+3158' for connections, or the airline name."""
    numbers = meta.get("flight_numbers") or []
    if numbers:
        carrier = numbers[0].split()[0]
        tail = "+".join(n.split()[-1] for n in numbers)
        return f"{carrier} {tail}"
    return meta.get("airline") or "Flight"


def _slices(meta: dict) -> list[dict]:
    """Journey slices (outbound / return). Falls back to the top-level
    fields for manifests written before multi-slice support."""
    slices = meta.get("slices")
    if isinstance(slices, list) and slices:
        return slices
    return [meta] if meta.get("origin") or meta.get("destination") else []


def _route_line(meta: dict) -> str:
    """'ORD → LAX', or 'ORD ⇄ LAX' for a round trip."""
    parts = _slices(meta)
    if not parts:
        return ""
    first, last = parts[0], parts[-1]
    origin = first.get("origin", "")
    dest = first.get("destination", "")
    if not (origin and dest):
        return ""
    if len(parts) > 1 and last.get("destination") == origin:
        return f"{_h(origin)} ⇄ {_h(dest)}"
    return f"{_h(origin)} → {_h(dest)}"


def _leg_details(leg: dict) -> str:
    """'May 8  ·  08:30 → 11:45  ·  1 stop via DEN' for one slice."""
    parts = []
    dep_date = leg.get("departure_date")
    if dep_date:
        parts.append(_h(_fmt_date(dep_date)))
    dep_t, arr_t = leg.get("departure_time"), leg.get("arrival_time")
    if dep_t and arr_t:
        parts.append(f"{_h(dep_t)} → {_h(arr_t)}")
    elif dep_t:
        parts.append(_h(dep_t))
    stops = leg.get("stops") or 0
    if stops:
        via = leg.get("via") or []
        via_str = f" via {_h('/'.join(via))}" if via else ""
        parts.append(f"{stops} stop{'s' if stops > 1 else ''}{via_str}")
    return "  ·  ".join(parts)


def _now_stamp() -> str:
    return datetime.now(TZ).strftime("%m/%d/%Y %H:%M")


# ── Price alerts ───────────────────────────────────────────────────────────────

def format_price_alert(
    quote: PriceQuote,
    meta: dict,
    is_new_flight: bool = False,
    last_known_price: int | None = None,
) -> str:
    if is_new_flight:
        lines = [f"✈️ <b>Now tracking</b>  ·  <b>${quote.price:,}</b>"]
    elif last_known_price is not None:
        saved = last_known_price - quote.price
        pct = round(saved / last_known_price * 100) if last_known_price else 0
        pct_str = f" · −{pct}%" if pct else ""
        lines = [
            f"📉 <b>New low!</b>  <b>${quote.price:,}</b>"
            f"  <i>(was ${last_known_price:,}, −${saved:,}{pct_str})</i>"
        ]
    else:
        lines = [f"📉 <b>New low!</b>  <b>${quote.price:,}</b>"]

    route = _route_line(meta)
    if route:
        lines.append(f"🛫 <b>{route}</b>")

    airline = meta.get("airline", "")
    label = _flight_label(meta)
    if meta.get("flight_numbers"):
        lines.append(
            f"{_h(airline)}  ·  <code>{_h(label)}</code>" if airline
            else f"<code>{_h(label)}</code>"
        )
    elif airline:
        lines.append(_h(airline))

    for leg in _slices(meta):
        details = _leg_details(leg)
        if details:
            lines.append(details)

    url = quote.search_url or meta.get("search_url")
    if url:
        lines.append(f'<a href="{_h(url)}">Open in Google Flights</a>')

    lines.append("")
    lines.append(f"<i>{_h(_now_stamp())}</i>")
    return "\n".join(lines)


def format_price_rise(quote: PriceQuote, meta: dict,
                      low_price: int | None) -> str:
    """Rebound off the all-time low — the discount window may be closing."""
    if low_price:
        risen = quote.price - low_price
        lines = [
            f"📈 <b>Rebounding</b>  ·  <b>${quote.price:,}</b>"
            f"  <i>(was at low ${low_price:,}, +${risen:,})</i>"
        ]
    else:
        lines = [f"📈 <b>Rebounding</b>  ·  <b>${quote.price:,}</b>"]

    route = _route_line(meta)
    if route:
        lines.append(f"🛫 <b>{route}</b>")
    label = _flight_label(meta)
    if meta.get("flight_numbers"):
        lines.append(f"<code>{_h(label)}</code>")

    url = quote.search_url or meta.get("search_url")
    if url:
        lines.append(f'<a href="{_h(url)}">Open in Google Flights</a>')

    lines.append("<i>The discount window may be closing.</i>")
    lines.append("")
    lines.append(f"<i>{_h(_now_stamp())}</i>")
    return "\n".join(lines)


def format_removed_flights(removed: dict) -> str:
    """One combined notice when saved flights disappear from Google Flights."""
    lines = [f"🗑️ <b>No longer tracking</b> ({len(removed)})",
             "<i>Removed from your Google Flights saved list.</i>", ""]
    for meta in removed.values():
        route = _route_line(meta) or "Unknown route"
        label = _flight_label(meta)
        dep = meta.get("departure_date", "")
        date_str = f"  ·  {_h(_fmt_date(dep))}" if dep else ""
        lines.append(f"  • {route}  <code>{_h(label)}</code>{date_str}")
    return "\n".join(lines)


# ── /status ────────────────────────────────────────────────────────────────────

# A run every 15 min means anything older than 3 cycles is an outage, not
# jitter. /status must never show a reassuring green over a dead tracker.
_STALE_AFTER_MINS = 45


def _staleness_banner(finished_at: str) -> str | None:
    try:
        finished = datetime.strptime(finished_at, "%m/%d/%Y %H:%M").replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None
    age_mins = (datetime.now(TZ) - finished).total_seconds() / 60
    if age_mins < _STALE_AFTER_MINS:
        return None
    age = (f"{age_mins / 1440:.1f} days" if age_mins >= 1440
           else f"{age_mins / 60:.1f} hours" if age_mins >= 90
           else f"{age_mins:.0f} min")
    return (
        f"🚨 <b>Last run was {age} ago</b> — runs are expected every "
        "~15 min. The tracker may be down; check CloudWatch."
    )


def format_status(summary: RunSummary | None, user: TelegramUser,
                  muted: bool, schedule_note: str = "",
                  failure_streak: int = 0) -> str:
    if summary is None:
        return (
            f"⏳ <b>No run data yet, {_h(user.name)}.</b>\n\n"
            "The tracker hasn't completed a run since deployment. "
            "It runs automatically on a schedule."
        )

    ok = "✅" if summary.ok else "❌"
    if summary.updated:
        upd_line = f"🔔 <b>{summary.updated}</b> new low{'s' if summary.updated != 1 else ''} found"
    else:
        upd_line = "📊 No price changes this run"

    lines = []
    stale = _staleness_banner(summary.finished_at)
    if stale:
        lines.extend([stale, ""])
    lines.extend([
        f"{ok} <b>Price Tracker</b>",
        "━━━━━━━━━━━━━━",
        f"🕐 {_h(summary.finished_at) or '—'}",
        f"✈️ <b>{summary.flights}</b> flights tracked",
        upd_line,
    ])
    if summary.removed:
        lines.append(f"🗑️ {summary.removed} removed")
    lines.append(f"⏱️ {summary.runtime_secs:.0f}s runtime")
    if not summary.ok and summary.error:
        lines.append(f"⚠️ <code>{_h(summary.error[:150])}</code>")
    if failure_streak:
        lines.append(f"🔥 <b>{failure_streak}</b> consecutive failed "
                     f"run{'s' if failure_streak != 1 else ''}")
    if muted:
        lines.append("")
        lines.append("🔕 <i>Your alerts are paused — /resume to re-enable.</i>")
    if schedule_note:
        lines.append(f"<i>{_h(schedule_note)}</i>")
    return "\n".join(lines)


# ── /flights ───────────────────────────────────────────────────────────────────

def format_flights(manifest: dict) -> str:
    flights = {
        fid: data for fid, data in manifest.items()
        if not fid.startswith("__") and isinstance(data, dict)
    }
    if not flights:
        return (
            "✈️ <b>No flights tracked yet.</b>\n\n"
            "Save flights on Google Flights and they'll appear here "
            "after the next run."
        )

    # Group by (departure_date, origin, destination); meta-less entries last
    groups: dict[tuple, list] = {}
    pending: list[tuple[str, dict]] = []
    for fid, meta in flights.items():
        if not (meta.get("origin") or meta.get("slices")):
            pending.append((fid, meta))
            continue
        first = _slices(meta)[0]
        key = (meta.get("departure_date") or first.get("departure_date") or "",
               first.get("origin", ""), first.get("destination", ""))
        groups.setdefault(key, []).append((fid, meta))

    lines = [f"✈️ <b>Monitored Flights</b> ({len(flights)})\n"]

    for (dep_date, _origin, _dest), group in sorted(groups.items()):
        header = f"🛫 <b>{_route_line(group[0][1])}</b>"
        if dep_date:
            header += f"  <i>{_h(_fmt_date(dep_date))}</i>"
        lines.append(header)

        group.sort(key=lambda item: item[1].get("departure_time") or "")
        for _fid, meta in group:
            label = _flight_label(meta)
            dep_t = meta.get("departure_time", "")
            arr_t = meta.get("arrival_time", "")
            time_str = f"{dep_t}→{arr_t}" if dep_t and arr_t else (dep_t or arr_t)

            stops = meta.get("stops") or 0
            if stops:
                via = meta.get("via") or []
                stop_str = f" · {stops} stop" + (f" {'/'.join(via)}" if via else "")
            else:
                stop_str = ""

            price = meta.get("price")
            low = meta.get("low_price")
            if price and low and price <= low:
                price_str = f" · <b>${price:,}</b> 🔥 <i>at the low</i>"
            elif price and low:
                price_str = f" · <b>${price:,}</b> <i>(low ${low:,})</i>"
            elif price:
                price_str = f" · <b>${price:,}</b>"
            else:
                price_str = ""

            url = meta.get("search_url", "")
            code = f"<code>{_h(label)}</code>"
            main = f'<a href="{_h(url)}">{code}</a>' if url else code
            lines.append(f"  • {main}  {_h(time_str)}{_h(stop_str)}{price_str}")

            # Round trips: the top-level times mirror the outbound leg only —
            # show the return so trips differing only by return are tellable
            # apart without a click-through.
            for leg in _slices(meta)[1:]:
                details = _leg_details(leg)
                if details:
                    lines.append(f"      ↩ {details}")
        lines.append("")

    if pending:
        lines.append("⏳ <i>Details pending</i>")
        for fid, meta in pending:
            price = meta.get("price")
            price_str = f"<b>${price:,}</b>" if price else "price unknown"
            lines.append(f"  • <code>{_h(fid[:14])}…</code>  {price_str}")
        lines.append("")

    updated = manifest.get("__updated__", "")
    if updated:
        lines.append(f"<i>Last updated: {_h(updated)}</i>")
    return "\n".join(lines)


# ── /help and misc ─────────────────────────────────────────────────────────────

def format_help(user: TelegramUser) -> str:
    return (
        f"👋 <b>Hi {_h(user.name)}!</b>\n\n"
        "<b>/status</b> — last run summary\n"
        "<b>/flights</b> — all monitored flights and prices\n"
        "<b>/settings</b> — your personal alert settings\n"
        "<b>/threshold 10</b> — only ping you for drops ≥ $10 "
        "(<b>/threshold 0</b> = every new low, the default)\n"
        "<b>/rises on</b> — also ping you when a price rebounds "
        "off its low (off by default)\n"
        "<b>/mute ORD LAX</b> — silence one route for you "
        "(<b>/unmute ORD LAX</b> to undo)\n"
        "<b>/pause</b> — stop <i>your</i> price alerts\n"
        "<b>/resume</b> — re-enable your alerts\n"
        "<b>/help</b> — this message\n\n"
        "<i>Alerts fire automatically whenever a saved flight hits "
        "a new all-time low. All settings are yours alone — they never "
        "affect the other user.</i>"
    )


def format_settings(user: TelegramUser, muted: bool, threshold: int,
                    rises: bool, muted_routes: set[str]) -> str:
    routes = (", ".join(sorted(r.replace("-", " → ") for r in muted_routes))
              if muted_routes else "none")
    return (
        f"⚙️ <b>Your settings, {_h(user.name)}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔔 Alerts: <b>{'paused' if muted else 'on'}</b>\n"
        f"📉 Drop threshold: <b>{f'${threshold:,}' if threshold else 'every new low'}</b>\n"
        f"📈 Rebound alerts: <b>{'on' if rises else 'off'}</b>\n"
        f"🔇 Muted routes: <b>{_h(routes)}</b>\n\n"
        "<i>/threshold N · /rises on|off · /mute A B — see /help</i>"
    )


def format_failure_notice(error: str, streak: int) -> str:
    return (
        f"❌ <b>Tracker has failed {streak} runs in a row</b>\n"
        f"<code>{_h(error[:300])}</code>\n"
        "<i>It will keep retrying on schedule. Check CloudWatch logs "
        "if this persists.</i>"
    )


def format_recovery_notice(streak: int) -> str:
    return (
        f"✅ <b>Tracker recovered</b> after {streak} failed "
        f"run{'s' if streak != 1 else ''}."
    )
