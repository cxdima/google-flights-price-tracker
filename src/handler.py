"""
Google Flights Price Tracker — AWS Lambda entry point.

Architecture:
  - Scheduled (EventBridge, every 10 min):
      1. Launch headless Chrome, authenticate, intercept GetSolutionPrices
      2. Kill Chrome immediately after extracting cookies + captured requests
      3. Re-fire API calls from pure Python (no browser needed)
      4. Compare prices against DynamoDB, notify Telegram on new lows
  - Telegram webhook (Function URL):
      Responds to /status and /flights.
"""
import json
import logging
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from config import (
    DYNAMODB_TABLE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, TZ,
)
from browser import build_driver, nuke_chrome
from auth import ensure_logged_in, _save_session
from tracker import (
    intercept_api_call, extract_flight_metadata,
    extract_params, build_session_headers, fetch_prices_with_headers, parse_prices,
)
from storage import (
    get_dynamodb, get_s3, get_last_price, write_price,
    update_flights_manifest, get_flights_manifest,
    save_last_summary, load_last_summary,
)
from notifier import send_price_alert, handle_webhook, _send

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("handler")

_last_summary: dict = {}


# ── Lambda entry point ─────────────────────────────────────────────────────────

def handler(event, context):
    body = event.get("body", "")
    if body:
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
            if "update_id" in parsed:
                return _handle_telegram_event(body)
        except Exception:
            pass
    return _run_tracker()


# ── Scheduled tracker ──────────────────────────────────────────────────────────

def _run_tracker() -> dict:
    global _last_summary
    t0 = time.monotonic()

    for attempt in range(1, 3):
        driver = None
        try:
            log.info("=== Attempt %d/2 ===", attempt)

            # ── Phase 1: Chrome (browser-dependent work) ─────────────────────
            nuke_chrome()
            driver = build_driver()

            s3  = get_s3()
            ddb = get_dynamodb()

            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": [
                "*google-analytics.com*",
                "*googletagmanager.com*",
                "*doubleclick.net*",
                "*googlesyndication.com*",
                "*google.com/gen_204*",
                "*gstatic.com/fonts/*",
                "*fonts.googleapis.com*",
            ]})

            ensure_logged_in(driver, s3)

            on_saves = "google.com/travel/flights/saves" in driver.current_url
            all_captured, page_html = intercept_api_call(
                driver, skip_nav=on_saves,
            )

            # Extract everything we need from the browser, then kill it.
            headers = build_session_headers(driver)
            _save_session(driver, s3)

            # ── Kill Chrome — free ~800 MB before HTTP/DB work ───────────────
            try:
                driver.quit()
            except Exception:
                pass
            driver = None
            _cleanup_tmp()
            log.info("Chrome released at %.1fs", time.monotonic() - t0)

            # ── Phase 2: pure Python (no browser) ────────────────────────────
            flight_meta = extract_flight_metadata(page_html)
            del page_html  # free the HTML string
            log.info("Metadata extracted for %d flight(s)", len(flight_meta))

            if not all_captured:
                raise RuntimeError("No GetSolutionPrices requests captured")

            # Re-fire captured API calls in parallel
            records_by_id: dict = {}
            log.info("Re-firing %d GetSolutionPrices request(s)", len(all_captured))

            with ThreadPoolExecutor(max_workers=min(len(all_captured), 4)) as pool:
                futs = {
                    pool.submit(fetch_prices_with_headers, extract_params(req), headers): i
                    for i, req in enumerate(all_captured)
                }
                for fut in as_completed(futs):
                    idx = futs[fut]
                    try:
                        raw = fut.result()
                        batch = parse_prices(raw)
                        log.info("Re-fire #%d: %d bytes → %d flight(s)", idx, len(raw), len(batch))
                        for rec in batch:
                            fid = rec["flight_id"]
                            if fid not in records_by_id or rec["price"] < records_by_id[fid]["price"]:
                                records_by_id[fid] = rec
                    except Exception as exc:
                        log.warning("Re-fire #%d failed: %s", idx, exc)

            records = sorted(records_by_id.values(), key=lambda r: r.get("price", 0))
            log.info("Parsed %d price record(s) from %d request(s)", len(records), len(all_captured))

            # ── Manifest update ──────────────────────────────────────────────
            manifest: dict = get_flights_manifest(s3)
            for fid, meta in flight_meta.items():
                manifest[fid] = {**manifest.get(fid, {}), **meta}
            for rec in records:
                fid = rec["flight_id"]
                manifest.setdefault(fid, {})
                manifest[fid]["price"] = rec["price"]
                if rec.get("search_url"):
                    manifest[fid]["search_url"] = rec["search_url"]
            manifest["__updated__"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
            update_flights_manifest(s3, manifest)

            # ── Compare & notify ─────────────────────────────────────────────
            if not records:
                log.warning("No price records parsed — nothing to compare")
                return _finish(s3, t0, attempt, flights=0, updated=0)

            table = ddb.Table(DYNAMODB_TABLE)

            # Fetch all last-known prices in parallel
            with ThreadPoolExecutor(max_workers=min(len(records), 8)) as pool:
                price_futs = {
                    pool.submit(get_last_price, table, rec["flight_id"]): rec["flight_id"]
                    for rec in records
                }
                last_prices = {}
                for fut in as_completed(price_futs):
                    fid = price_futs[fut]
                    try:
                        last_prices[fid] = fut.result()
                    except Exception:
                        last_prices[fid] = None

            # Determine which flights have new lows, then write + notify in parallel
            updates: list[tuple] = []
            for rec in records:
                fid  = rec["flight_id"]
                meta = flight_meta.get(fid, {})
                last = last_prices.get(fid)

                last_price = int(last["price"]) if last else None
                new_price  = int(rec["price"])

                if last_price is None or new_price < last_price:
                    updates.append((rec, meta, last_price))
                    log.info("New low for %s: $%d (was $%s)", fid[:16], new_price,
                             str(last_price) if last_price else "none")
                else:
                    log.info("No change for %s: $%d (min $%d)", fid[:16], new_price, last_price)

            if updates:
                with ThreadPoolExecutor(max_workers=min(len(updates) * 2, 8)) as pool:
                    for rec, meta, last_price in updates:
                        pool.submit(write_price, table, rec, meta)
                        pool.submit(
                            send_price_alert, rec, meta,
                            last_price is None, last_price,
                        )

            return _finish(s3, t0, attempt, flights=len(records), updated=len(updates))

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Attempt %d failed: %s\n%s", attempt, exc, tb)
            if attempt == 2:
                _notify_error(str(exc), tb)
                return {"ok": False, "error": str(exc)}

        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            _cleanup_tmp()

    return {"ok": False, "error": "exhausted retries"}


def _finish(s3, t0: float, attempt: int, flights: int, updated: int) -> dict:
    global _last_summary
    runtime = time.monotonic() - t0
    summary = {
        "ok":           True,
        "attempt":      attempt,
        "flights":      flights,
        "updated":      updated,
        "runtime_secs": round(runtime, 1),
        "ts":           datetime.now(TZ).strftime("%m/%d/%Y %H:%M"),
    }
    _last_summary = summary
    save_last_summary(s3, summary)
    log.info("GFPT_SUMMARY %s", json.dumps(summary))
    return summary


# ── Telegram webhook ───────────────────────────────────────────────────────────

def _handle_telegram_event(body: str) -> dict:
    s3       = get_s3()
    summary  = _last_summary or load_last_summary(s3)
    manifest = get_flights_manifest(s3)
    result   = handle_webhook(body, last_run_summary=summary, flights_manifest=manifest)
    return {"statusCode": 200, "body": json.dumps(result)}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _notify_error(error: str, tb: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
    short_tb = tb.splitlines()[-3:]
    error_safe = _html_escape(error[:200])
    tb_safe = _html_escape("\n".join(short_tb))
    text = (
        "❌ <b>gfpricetracker crashed</b>\n"
        f"<code>{error_safe}</code>\n"
        f"<pre>{tb_safe}</pre>"
    )
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            _send(chat_id, text, parse_mode="HTML")
        except Exception:
            pass


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cleanup_tmp() -> None:
    import glob
    for d in glob.glob("/tmp/gfpt-chrome-*"):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
