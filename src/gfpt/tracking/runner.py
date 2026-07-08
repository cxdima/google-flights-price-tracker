"""
One tracker run, orchestrated:

  Phase 1 (Chrome alive):   login → intercept GetSolutionPrices → grab
                            cookies/UA → save session → kill Chrome
  Phase 2 (plain Python):   re-fire API calls → parse prices → reconcile
                            manifest (prune stale flights) → compare with
                            DynamoDB → alert on new lows

Chrome is shut down as early as possible: it holds ~800 MB that phase 2
doesn't need.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

from gfpt.bot.notifier import Notifier
from gfpt.config import FAILURE_NOTIFY_THRESHOLD, TZ, Settings
from gfpt.models import PriceQuote, RunSummary
from gfpt.storage.dynamo import PriceHistory
from gfpt.storage.state import StateStore
from gfpt.tracking.auth import ensure_logged_in, save_session
from gfpt.tracking.browser import build_driver, cleanup_profiles, nuke_chrome, quit_quietly
from gfpt.tracking.capture import build_session_headers, intercept_api_call
from gfpt.tracking.metadata import extract_flight_metadata
from gfpt.tracking.prices import extract_params, fetch_prices, parse_prices
from gfpt.tracking.reconcile import reconcile_manifest

log = logging.getLogger(__name__)

# Third-party noise blocked at the network layer — faster page loads and
# fewer tracking beacons from an automated browser.
_BLOCKED_URLS = [
    "*google-analytics.com*",
    "*googletagmanager.com*",
    "*doubleclick.net*",
    "*googlesyndication.com*",
    "*google.com/gen_204*",
    "*gstatic.com/fonts/*",
    "*fonts.googleapis.com*",
]

_MAX_REFIRE_WORKERS = 4


@dataclass(frozen=True)
class TrackerDeps:
    settings: Settings
    state: StateStore
    history: PriceHistory
    notifier: Notifier


def run_and_report(deps: TrackerDeps) -> dict:
    """
    Execute one run, persist the summary, and maintain the failure streak.

    A single attempt per invocation: the schedule provides retries, and a
    second in-process attempt rarely fit inside the Lambda timeout anyway.
    Users are notified once after FAILURE_NOTIFY_THRESHOLD consecutive
    failures (not on every blip), and once on recovery.
    """
    t0 = time.monotonic()
    try:
        summary = _run(deps, t0)
    except Exception as exc:
        log.error("Tracker run failed: %s\n%s", exc, traceback.format_exc())
        summary = RunSummary(
            ok=False,
            error=str(exc)[:300],
            runtime_secs=time.monotonic() - t0,
            finished_at=_now_str(),
        )
        deps.state.save_summary(summary.to_dict())
        streak = deps.state.load_failure_streak() + 1
        deps.state.save_failure_streak(streak)
        if streak == FAILURE_NOTIFY_THRESHOLD:
            deps.notifier.failure(str(exc), streak)
        return summary.to_dict()

    deps.state.save_summary(summary.to_dict())
    streak = deps.state.load_failure_streak()
    if streak:
        deps.state.save_failure_streak(0)
        if streak >= FAILURE_NOTIFY_THRESHOLD:
            deps.notifier.recovery(streak)
    log.info("GFPT_SUMMARY %s", json.dumps(summary.to_dict()))
    return summary.to_dict()


def _run(deps: TrackerDeps, t0: float) -> RunSummary:
    captured, page_html, headers = _browser_phase(deps.settings, deps.state)
    log.info("Chrome released at %.1fs", time.monotonic() - t0)

    flight_meta = extract_flight_metadata(page_html)
    del page_html  # free the multi-MB HTML string

    quotes = _fetch_quotes(captured, headers)
    log.info("Parsed %d price quote(s) from %d request(s)",
             len(quotes), len(captured))
    if not quotes:
        raise RuntimeError("No price quotes parsed from captured requests")

    # ── Reconcile manifest (adds new flights, prunes removed ones) ────────────
    result = reconcile_manifest(
        deps.state.load_manifest(), flight_meta, quotes, updated_at=_now_str(),
    )
    deps.state.save_manifest(result.manifest)
    if result.removed:
        deps.notifier.flights_removed(result.removed)

    # ── Compare against history and alert on new lows ─────────────────────────
    updated = _process_quotes(quotes, result.manifest, deps)

    return RunSummary(
        ok=True,
        flights=len(quotes),
        updated=updated,
        removed=len(result.removed),
        runtime_secs=time.monotonic() - t0,
        finished_at=_now_str(),
    )


# ── Phase 1: browser ───────────────────────────────────────────────────────────

def _browser_phase(settings: Settings, state: StateStore) -> tuple[list, str, dict]:
    nuke_chrome()
    driver = build_driver(settings)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": _BLOCKED_URLS})

        ensure_logged_in(driver, settings, state)

        # Session restore already landed on the saves page with CDP running —
        # don't reload, the price requests we need may already be in the log.
        on_saves = "google.com/travel/flights/saves" in driver.current_url
        captured, page_html = intercept_api_call(
            driver, settings.hydrate_secs, skip_nav=on_saves,
        )

        headers = build_session_headers(driver)
        save_session(driver, state)
        return captured, page_html, headers
    finally:
        quit_quietly(driver)  # frees ~800 MB before phase 2
        cleanup_profiles()


# ── Phase 2: pure Python ───────────────────────────────────────────────────────

def _fetch_quotes(captured: list[dict], headers: dict) -> list[PriceQuote]:
    """Re-fire all captured requests concurrently; merge to the lowest
    price per flight."""
    best: dict[str, PriceQuote] = {}
    workers = min(len(captured), _MAX_REFIRE_WORKERS)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_prices, extract_params(req), headers): i
            for i, req in enumerate(captured)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                raw = future.result()
            except Exception as exc:
                log.warning("Re-fire #%d failed: %s", idx, exc)
                continue
            batch = parse_prices(raw)
            log.info("Re-fire #%d: %d bytes → %d flight(s)", idx, len(raw), len(batch))
            for quote in batch:
                existing = best.get(quote.flight_id)
                if existing is None or quote.price < existing.price:
                    best[quote.flight_id] = quote

    return sorted(best.values(), key=lambda q: q.price)


def _process_quotes(quotes: list[PriceQuote], manifest: dict,
                    deps: TrackerDeps) -> int:
    """Record and alert every new low. Returns the number of updates."""
    updated = 0
    for quote in quotes:
        meta = manifest.get(quote.flight_id, {})
        last_price = deps.history.last_price(quote.flight_id)

        if last_price is not None and quote.price >= last_price:
            log.info("No change for %s: $%d (min $%d)",
                     quote.flight_id[:16], quote.price, last_price)
            continue

        is_new = last_price is None
        log.info("New %s for %s: $%d%s",
                 "flight" if is_new else "low", quote.flight_id[:16],
                 quote.price, "" if is_new else f" (was ${last_price})")
        deps.history.record(quote, meta, prev_price=last_price)
        deps.notifier.price_alert(
            quote, meta, is_new_flight=is_new, last_known_price=last_price,
        )
        updated += 1
    return updated


def _now_str() -> str:
    return datetime.now(TZ).strftime("%m/%d/%Y %H:%M")
