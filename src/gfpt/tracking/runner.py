"""
One tracker run, orchestrated:

  Phase 1 (Chrome alive):   login → intercept GetSolutionPrices → grab
                            cookies/UA → save session → kill Chrome
  Phase 2 (plain Python):   re-fire API calls → parse prices → reconcile
                            manifest (prune stale flights) → compare with
                            the low-price watermark → alert on new lows

Chrome is shut down as early as possible: it holds ~800 MB that phase 2
doesn't need.

Alert semantics (the product): a NEW ALL-TIME LOW pings every unmuted user
whose personal threshold allows it (default threshold 0 = every new low).
Price rises are recorded for history but only ping users who opted in via
/rises, and only when a price rebounds off its low — the "discount window
is closing" signal.
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
from gfpt.tracking.auth import LoginFailedError, ensure_logged_in, save_session
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


@dataclass(frozen=True)
class PriceEvent:
    """One user-visible price movement detected this run."""
    kind: str                 # "new" | "low" | "rise"
    quote: PriceQuote
    meta: dict                # manifest entry (pre-update watermark in prev_low)
    prev_low: int | None      # watermark before this run (None for new flights)


def run_and_report(deps: TrackerDeps) -> dict:
    """
    Execute one run, persist the summary, and maintain the failure streak.

    A single attempt per invocation: the schedule provides retries, and a
    second in-process attempt rarely fit inside the Lambda timeout anyway.
    Users are notified once after FAILURE_NOTIFY_THRESHOLD consecutive
    failures (not on every blip), and once on recovery.

    The streak is written AHEAD of the run (and reset on success): a process
    killed by the Lambda timeout or OOM — which never reaches an except
    block — still counts as a failure on the next invocation.
    """
    t0 = time.monotonic()
    health = deps.state.load_health()
    prev_streak = _as_int(health.get("consecutive_failures"))
    streak = prev_streak + 1
    deps.state.save_health({**health, "consecutive_failures": streak})

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

        new_health = {**health, "consecutive_failures": streak}
        if isinstance(exc, LoginFailedError):
            new_health["login_failures"] = _as_int(health.get("login_failures")) + 1
        if streak >= FAILURE_NOTIFY_THRESHOLD and not health.get("failure_notified"):
            # Set the flag only on confirmed delivery — a dropped Telegram
            # send at exactly the threshold must be retried next run.
            if deps.notifier.failure(str(exc), streak):
                new_health["failure_notified"] = True
        deps.state.save_health(new_health)
        # Emitted on BOTH paths: the CloudWatch metric filter alarms on
        # `"ok": false` lines, independent of S3 and Telegram.
        log.info("GFPT_SUMMARY %s", json.dumps(summary.to_dict()))
        return summary.to_dict()

    deps.state.save_summary(summary.to_dict())
    deps.state.save_health(
        {"consecutive_failures": 0, "failure_notified": False, "login_failures": 0}
    )
    if health.get("failure_notified"):
        deps.notifier.recovery(prev_streak)
    log.info("GFPT_SUMMARY %s", json.dumps(summary.to_dict()))
    return summary.to_dict()


def _run(deps: TrackerDeps, t0: float) -> RunSummary:
    captured, page_html, headers = _browser_phase(deps.settings, deps.state)
    log.info("Chrome released at %.1fs", time.monotonic() - t0)

    flight_meta = extract_flight_metadata(page_html)
    del page_html  # free the multi-MB HTML string

    quotes, refire_failures = _fetch_quotes(captured, headers)
    log.info("Parsed %d price quote(s) from %d request(s), %d re-fire failure(s)",
             len(quotes), len(captured), refire_failures)
    if not quotes:
        raise RuntimeError("No price quotes parsed from captured requests")

    old_manifest = deps.state.load_manifest()
    prev_quoted = {
        fid: _as_int(entry.get("price"), default=None)
        for fid, entry in old_manifest.items()
        if isinstance(entry, dict)
    }

    # A failed re-fire batch or a dead metadata parser means this run's view
    # is partial — "absent" is meaningless, so pruning must not advance.
    old_has_flights = any(
        not fid.startswith("__") and isinstance(e, dict)
        for fid, e in old_manifest.items()
    )
    partial = bool(refire_failures) or (old_has_flights and not flight_meta)
    if partial:
        log.warning("Partial run — pruning suspended for this cycle")

    result = reconcile_manifest(
        old_manifest, flight_meta, quotes,
        updated_at=_now_str(), prune_allowed=not partial,
    )

    events, manifest = _process_quotes(quotes, result.manifest, prev_quoted, deps)

    # The watermark must be durable before anyone is alerted on it; a failed
    # write here also routes S3 breakage into the failure streak instead of
    # silently degrading state.
    if not deps.state.save_manifest(manifest):
        raise RuntimeError("S3 manifest write failed")

    if result.removed:
        deps.notifier.flights_removed(result.removed)
    updated = _dispatch_events(events, deps)

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

def _fetch_quotes(captured: list[dict], headers: dict) -> tuple[list[PriceQuote], int]:
    """Re-fire all captured requests concurrently; merge to the lowest
    price per flight. Returns (quotes, number_of_failed_refires)."""
    best: dict[str, PriceQuote] = {}
    failures = 0
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
                failures += 1
                continue
            batch = parse_prices(raw)
            log.info("Re-fire #%d: %d bytes → %d flight(s)", idx, len(raw), len(batch))
            for quote in batch:
                existing = best.get(quote.flight_id)
                if existing is None or quote.price < existing.price:
                    best[quote.flight_id] = quote

    return sorted(best.values(), key=lambda q: q.price), failures


def _process_quotes(
    quotes: list[PriceQuote],
    manifest: dict,
    prev_quoted: dict[str, int | None],
    deps: TrackerDeps,
) -> tuple[list[PriceEvent], dict]:
    """
    Compare each quote against its manifest low-price watermark, update the
    watermark, and record every CHANGED price to history (rises included —
    they make future trend features possible; only new lows and rebounds
    become alerts).

    Returns (events_to_dispatch, manifest_with_updated_watermarks).
    """
    out = dict(manifest)
    events: list[PriceEvent] = []

    for quote in quotes:
        entry = dict(out.get(quote.flight_id) or {})
        prev = prev_quoted.get(quote.flight_id)
        low = _as_int(entry.get("low_price"), default=None)

        if low is None:
            # No watermark yet (first run since the feature, or a brand-new
            # flight): seed from DynamoDB history once. A failed read must
            # not look like "never seen" — skip the flight; the next run
            # retries for free.
            try:
                low = deps.history.last_price(quote.flight_id)
            except LookupError:
                log.warning("History read failed for %s — skipping this run",
                            quote.flight_id[:16])
                continue
            if low is not None:
                # Persist the seed even when the price is unchanged this
                # run — otherwise the DynamoDB fallback repeats every run
                # and /flights never learns the low.
                entry["low_price"] = low

        if low is None:
            log.info("New flight %s at $%d", quote.flight_id[:16], quote.price)
            entry["low_price"] = quote.price
            entry["low_ts"] = _now_str()
            entry.pop("rebounded", None)
            deps.history.record(quote, entry, prev_price=None)
            events.append(PriceEvent("new", quote, entry, prev_low=None))
        elif quote.price < low:
            log.info("New low for %s: $%d (was $%d)",
                     quote.flight_id[:16], quote.price, low)
            entry["low_price"] = quote.price
            entry["low_ts"] = _now_str()
            entry.pop("rebounded", None)
            deps.history.record(quote, entry, prev_price=low)
            events.append(PriceEvent("low", quote, entry, prev_low=low))
        elif prev is not None and quote.price > prev:
            # Any rise is recorded; only a rebound OFF THE LOW is an event —
            # the one rise that means "the discount window is closing".
            # Damped to once per low: a price flapping on and off its low
            # must not ping rise-subscribers on every bounce.
            deps.history.record(quote, entry, prev_price=prev)
            if prev == low and not entry.get("rebounded"):
                log.info("Rebound for %s: $%d off low $%d",
                         quote.flight_id[:16], quote.price, low)
                entry["rebounded"] = True
                events.append(PriceEvent("rise", quote, entry, prev_low=low))
        elif prev is not None and quote.price != prev:
            # Changed but not a new low or rise-from-prev (e.g. drop that
            # stays above the watermark) — history only, no alert.
            deps.history.record(quote, entry, prev_price=prev)

        out[quote.flight_id] = entry

    return events, out


def _dispatch_events(events: list[PriceEvent], deps: TrackerDeps) -> int:
    """Send alerts after the manifest is durable. Returns alert count
    (new flights + new lows; rebounds are opt-in and not counted)."""
    updated = 0
    for ev in events:
        if ev.kind == "rise":
            deps.notifier.price_rise(ev.quote, ev.meta, low_price=ev.prev_low)
            continue
        deps.notifier.price_alert(
            ev.quote, ev.meta,
            is_new_flight=(ev.kind == "new"),
            last_known_price=ev.prev_low,
        )
        updated += 1
    return updated


def _as_int(value, default: int | None = 0) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now_str() -> str:
    return datetime.now(TZ).strftime("%m/%d/%Y %H:%M")
