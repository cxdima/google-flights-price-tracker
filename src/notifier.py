"""
Telegram integration — price alerts, /status replies, and webhook routing.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, TZ

log = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org"
_AUTHORIZED_IDS = set(TELEGRAM_CHAT_IDS)


# ── Price alert ────────────────────────────────────────────────────────────────

def build_price_message(
    record: dict,
    meta: dict,
    is_new_flight: bool = False,
    last_known_price: int | None = None,
) -> str:
    """
    Build an HTML Telegram message for a price-change / new-low event.

    record keys: flight_id, price, prev_price, price_change
    meta   keys: airline, flight_numbers, origin, destination,
                 departure_date, departure_time, arrival_time,
                 stops, via, duration_min
    is_new_flight    — True when this flight has never been tracked before
    last_known_price — previous minimum price stored in the DB (if any)
    """
    price = record.get("price", 0)

    # ── Headline ───────────────────────────────────────────────────────────────
    if is_new_flight:
        lines = [f"✈️ <b>Now tracking</b>  ·  <b>${price:,}</b>"]
    else:
        if last_known_price is not None:
            saved = last_known_price - price
            lines = [
                f"📉 <b>New low!</b>  <b>${price:,}</b>"
                f"  <i>(was ${last_known_price:,}, −${saved:,})</i>"
            ]
        else:
            lines = [f"📉 <b>New low!</b>  <b>${price:,}</b>"]

    # ── Route ─────────────────────────────────────────────────────────────────
    origin = meta.get("origin", "")
    dest   = meta.get("destination", "")
    if origin and dest:
        lines.append(f"🛫 <b>{_h(origin)} → {_h(dest)}</b>")

    # ── Carrier + flight numbers ───────────────────────────────────────────────
    fnum_list = meta.get("flight_numbers") or []
    airline   = meta.get("airline", "")
    if fnum_list:
        nums       = "+".join(fn.split()[-1] for fn in fnum_list)
        carrier    = fnum_list[0].split()[0]
        flight_str = f"{carrier} {nums}"
        carrier_line = (
            f"{_h(airline)}  ·  <code>{_h(flight_str)}</code>"
            if airline else f"<code>{_h(flight_str)}</code>"
        )
        lines.append(carrier_line)
    elif airline:
        lines.append(_h(airline))

    # ── Date / time / stops ────────────────────────────────────────────────────
    dep_date = meta.get("departure_date", "")
    dep_t    = meta.get("departure_time", "")
    arr_t    = meta.get("arrival_time", "")
    stops    = meta.get("stops", 0)
    via      = meta.get("via") or []

    parts = []
    if dep_date:
        parts.append(_h(_fmt_date(dep_date)))
    if dep_t and arr_t:
        parts.append(f"{_h(dep_t)} → {_h(arr_t)}")
    if stops:
        via_str = " via " + _h("/".join(via)) if via else ""
        parts.append(f"{stops} stop{via_str}")

    if parts:
        lines.append("  ·  ".join(parts))

    # ── Timestamp ─────────────────────────────────────────────────────────────
    lines.append("")
    lines.append(f"<i>{_h(datetime.now(TZ).strftime('%m/%d/%Y %H:%M'))}</i>")

    return "\n".join(lines)


# ── Sending ────────────────────────────────────────────────────────────────────

def send_price_alert(
    record: dict,
    meta: dict,
    is_new_flight: bool = False,
    last_known_price: int | None = None,
) -> None:
    """Send a price alert to all configured Telegram chat IDs."""
    text = build_price_message(record, meta, is_new_flight=is_new_flight, last_known_price=last_known_price)
    _broadcast(text, parse_mode="HTML")


def _broadcast(text: str, parse_mode: str = "HTML") -> None:
    """Send `text` to every chat ID in TELEGRAM_CHAT_IDS."""
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — skipping notification")
        return
    for chat_id in TELEGRAM_CHAT_IDS:
        _send(chat_id, text, parse_mode)


def _send(chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    """POST a message to a single Telegram chat. Returns True on success."""
    url = f"{_TG_API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                return True
            log.warning("Telegram API error: %s", result.get("description"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        log.warning("Telegram HTTP %s: %s", exc.code, body[:200])
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
    return False


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _h(text) -> str:
    """Escape text for Telegram HTML mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_date(date_str: str) -> str:
    """Convert '2026-05-08' → 'May 8'."""
    try:
        from datetime import date as _date
        d = _date.fromisoformat(date_str)
        return d.strftime("%b %-d")   # e.g. "May 8"  (Linux/macOS)
    except Exception:
        return date_str


# ── Webhook (Telegram → Lambda) ────────────────────────────────────────────────

def handle_webhook(
    body: str,
    last_run_summary: dict | None = None,
    flights_manifest: dict | None = None,
) -> dict:
    """
    Parse an incoming Telegram webhook payload and respond to known commands.

    Supported commands:
      /status  — last tracker run summary
      /flights — full list of monitored flights with prices
    """
    try:
        update = json.loads(body)
    except Exception:
        return {"ok": False, "error": "invalid JSON"}

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    text    = message.get("text", "").strip().lower()

    if chat_id not in _AUTHORIZED_IDS:
        log.info("Ignoring webhook from unauthorized chat %s", chat_id)
        return {"ok": True, "ignored": True}

    if text.startswith("/status"):
        _send_status(chat_id, last_run_summary)
    elif text.startswith("/flights"):
        _send_flights(chat_id, flights_manifest or {})

    return {"ok": True}


def _send_status(chat_id: str, summary: dict | None) -> None:
    """Reply to /status with the last run summary (HTML formatted)."""
    if not summary:
        msg = (
            "⏳ <b>No run data yet.</b>\n\n"
            "The tracker hasn't completed a run since the bot started.\n"
            "It runs every 10 minutes automatically."
        )
        _send(chat_id, msg, parse_mode="HTML")
        return

    ok      = "✅" if summary.get("ok") else "❌"
    ts      = _h(summary.get("ts", "—"))
    flights = summary.get("flights", 0)
    updated = summary.get("updated", 0)
    runtime = summary.get("runtime_secs", 0)
    attempt = summary.get("attempt", 1)

    attempt_note = f"  <i>(retry #{attempt})</i>" if attempt > 1 else ""
    if updated:
        upd_line = f"🔔 <b>{updated}</b> new low{'s' if updated != 1 else ''} found"
    else:
        upd_line = "📊 No price changes this run"

    msg = (
        f"{ok} <b>Price Tracker{attempt_note}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🕐 {ts}\n"
        f"✈️ <b>{flights}</b> flights tracked\n"
        f"{upd_line}\n"
        f"⏱️ {runtime:.0f}s runtime"
    )
    _send(chat_id, msg, parse_mode="HTML")


def _send_flights(chat_id: str, manifest: dict) -> None:
    """Reply to /flights with the full monitored-flights list (HTML formatted)."""
    msg = build_flights_message(manifest)
    _send(chat_id, msg, parse_mode="HTML")


def build_flights_message(manifest: dict) -> str:
    """
    Build an HTML Telegram message listing all monitored flights grouped by
    route and date.
    """
    # Strip internal metadata keys
    flights = {
        fid: data
        for fid, data in manifest.items()
        if not fid.startswith("__") and isinstance(data, dict)
    }

    if not flights:
        return (
            "✈️ <b>No flights in manifest yet.</b>\n\n"
            "The tracker will populate this list on its next run."
        )

    # Group by (departure_date, origin, destination)
    groups: dict = {}
    for fid, meta in flights.items():
        origin  = meta.get("origin", "???")
        dest    = meta.get("destination", "???")
        dep_date = meta.get("departure_date", "")
        key = (dep_date, origin, dest)
        groups.setdefault(key, []).append((fid, meta))

    sorted_groups = sorted(groups.items())

    lines = [f"✈️ <b>Monitored Flights</b> ({len(flights)})\n"]

    for (dep_date, origin, dest), group_flights in sorted_groups:
        date_str  = _fmt_date(dep_date) if dep_date else ""
        route_hdr = f"🛫 <b>{_h(origin)} → {_h(dest)}</b>"
        if date_str:
            route_hdr += f"  <i>{_h(date_str)}</i>"
        lines.append(route_hdr)

        # Sort by departure time within the group
        group_flights.sort(key=lambda x: x[1].get("departure_time", ""))

        for _fid, meta in group_flights:
            fnum_list = meta.get("flight_numbers") or []
            # "WN 3492" → carrier="WN", num="3492"
            # multiple legs: "WN 2164", "WN 3158" → "WN 2164+3158"
            if fnum_list:
                carrier = fnum_list[0].split()[0]
                nums    = "+".join(fn.split()[-1] for fn in fnum_list)
                flight_str = f"{carrier} {nums}"
            else:
                flight_str = meta.get("airline", "?")

            dep_t = meta.get("departure_time", "")
            arr_t = meta.get("arrival_time", "")
            time_str = f"{dep_t}→{arr_t}" if dep_t and arr_t else dep_t or arr_t

            stops = meta.get("stops", 0)
            if stops:
                via      = meta.get("via") or []
                via_str  = "/".join(via) if via else ""
                stop_str = f" · {stops} stop" + (f" {_h(via_str)}" if via_str else "")
            else:
                stop_str = ""

            price = meta.get("price")
            price_str = f" · <b>${price:,}</b>" if price else ""

            search_url = meta.get("search_url", "")
            if search_url:
                main = f'<a href="{_h(search_url)}"><code>{_h(flight_str)}</code></a>'
            else:
                main = f"<code>{_h(flight_str)}</code>"

            lines.append(f"  • {main}  {_h(time_str)}{_h(stop_str)}{price_str}")

        lines.append("")  # blank line between route groups

    updated = manifest.get("__updated__", "")
    if updated:
        lines.append(f"<i>Last updated: {_h(updated)}</i>")

    return "\n".join(lines)
