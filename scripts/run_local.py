#!/usr/bin/env python3
"""
Manual integration harness — runs the full price-tracking pipeline against
live Chrome.

Each stage can be executed independently so you can pinpoint failures quickly.
The browser window is visible by default for easy debugging.

Usage:
  python scripts/run_local.py                   # run all stages
  python scripts/run_local.py --stage auth      # single stage
  python scripts/run_local.py --headless        # headless Chrome
  python scripts/run_local.py --stage prices --headless

Stages (in order):
  browser    Launch Chrome and verify stealth patches are applied
  auth       Restore session from S3 or perform a full login
  intercept  Capture the GetSolutionPrices CDP network request
  prices     Re-fire the API call and parse price records
  storage    Query DynamoDB for historical price data (read-only)
"""
import argparse
import logging
import os
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

# ── Path bootstrap ─────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ── Load .env ──────────────────────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

from gfpt.config import CHROME_USER_AGENT, load_settings
from gfpt.storage.dynamo import PriceHistory
from gfpt.storage.state import StateStore
from gfpt.tracking.auth import ensure_logged_in, is_authenticated
from gfpt.tracking.browser import apply_stealth, build_driver, nuke_chrome
from gfpt.tracking.capture import build_session_headers, intercept_api_call
from gfpt.tracking.metadata import extract_flight_metadata
from gfpt.tracking.prices import (
    MAX_PLAUSIBLE_PRICE,
    MIN_PLAUSIBLE_PRICE,
    extract_params,
    fetch_prices,
    parse_prices,
)

# Settings are read AFTER .env has been loaded into the environment.
settings = load_settings()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("run_local")

# ── ANSI colours ───────────────────────────────────────────────────────────────
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

PASS = f"{_GREEN}PASS{_RESET}"
FAIL = f"{_RED}FAIL{_RESET}"
SKIP = f"{_YELLOW}SKIP{_RESET}"


# ── Stage result tracking ──────────────────────────────────────────────────────

@dataclass
class StageResult:
    name:       str
    passed:     bool
    elapsed_s:  float
    detail:     str = ""
    error:      str = ""


_results: list[StageResult] = []


def _run_stage(name: str, fn: Callable) -> StageResult:
    """Execute fn(), record timing and pass/fail status."""
    print(f"\n{_BOLD}── {name} {'─' * (50 - len(name))}{_RESET}")
    t0 = time.monotonic()
    try:
        detail = fn() or ""
        elapsed = time.monotonic() - t0
        r = StageResult(name=name, passed=True, elapsed_s=elapsed, detail=str(detail))
        print(f"  {PASS}  ({elapsed:.1f}s)  {detail}")
    except Exception as exc:
        elapsed = time.monotonic() - t0
        tb = traceback.format_exc()
        r = StageResult(name=name, passed=False, elapsed_s=elapsed, error=str(exc))
        print(f"  {FAIL}  ({elapsed:.1f}s)  {exc}")
        print()
        for line in tb.splitlines()[-6:]:
            print(f"    {line}")
    _results.append(r)
    return r


def _print_summary():
    print(f"\n{_BOLD}{'═' * 60}{_RESET}")
    print(f"{_BOLD}  Results{_RESET}")
    print(f"{'─' * 60}")
    total = len(_results)
    passed = sum(1 for r in _results if r.passed)
    for r in _results:
        status = PASS if r.passed else FAIL
        print(f"  {status}  {r.name:<20}  {r.elapsed_s:>5.1f}s  {r.detail or r.error}")
    print(f"{'─' * 60}")
    print(f"  {passed}/{total} stages passed")
    print(f"{'═' * 60}\n")


# ── Build a visible (non-headless) Chrome driver for local debugging ──────────

def _build_visible_driver():
    from selenium import webdriver as wd
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service as ChromeService

    opts = Options()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-agent={CHROME_USER_AGENT}")
    opts.add_argument("--window-size=1280,900")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    opts.page_load_strategy = "eager"

    svc    = ChromeService(executable_path=settings.chromedriver_path)
    driver = wd.Chrome(service=svc, options=opts)
    apply_stealth(driver)
    return driver


# ── Individual stages ─────────────────────────────────────────────────────────

def stage_browser(driver):
    """Verify the stealth patches are active in the launched browser."""
    # navigator.webdriver should be undefined (not True)
    webdriver_val = driver.execute_script("return navigator.webdriver")
    assert webdriver_val is None or webdriver_val is False, (
        f"navigator.webdriver is {webdriver_val!r} — stealth patch may have failed"
    )

    # window.chrome should exist
    chrome_exists = driver.execute_script("return typeof window.chrome !== 'undefined'")
    assert chrome_exists, "window.chrome is missing — stealth patch failed"

    # navigator.plugins should have entries
    plugin_count = driver.execute_script("return navigator.plugins.length")
    assert plugin_count > 0, f"navigator.plugins.length == {plugin_count}"

    ua = driver.execute_script("return navigator.userAgent")
    assert "HeadlessChrome" not in ua, f"User-agent exposes headless: {ua}"

    return f"webdriver=undefined  chrome=present  plugins={plugin_count}  ua OK"


def stage_auth(driver, state):
    """Ensure we are authenticated on Google Flights."""
    ensure_logged_in(driver, settings, state)
    assert is_authenticated(driver), (
        f"Not authenticated after ensure_logged_in — URL: {driver.current_url}"
    )
    return f"authenticated  url={driver.current_url[:60]}"


def stage_intercept(driver):
    """Intercept the GetSolutionPrices network call."""
    all_captured, page_html = intercept_api_call(driver, settings.hydrate_secs)

    assert all_captured, "No requests captured"
    for req in all_captured:
        assert "GetSolutionPrices" in req.get("url", ""), (
            f"Unexpected URL: {req.get('url', '')[:80]}"
        )
        assert req.get("postData"), "Captured request has no postData"
    assert len(page_html) > 500, "Page HTML looks too short"

    meta = extract_flight_metadata(page_html)
    flight_count = len(meta)

    return all_captured, page_html, f"{len(all_captured)} request(s), {flight_count} flight(s) in metadata"


def stage_prices(driver, all_captured, page_html):
    """Re-fire captured requests and parse the returned prices."""
    headers = build_session_headers(driver)
    records = []
    for req in all_captured:
        params = extract_params(req)
        body   = fetch_prices(params, headers)
        records.extend(parse_prices(body))

    assert isinstance(records, list), "parse_prices did not return a list"
    assert len(records) > 0, "No price records found — check your saved flights"

    for r in records:
        assert r.flight_id, f"Record missing flight_id: {r}"
        assert r.price is not None, f"Record missing price: {r}"
        assert MIN_PLAUSIBLE_PRICE <= r.price <= MAX_PLAUSIBLE_PRICE, (
            f"Price out of expected range: {r.price}"
        )

    cheapest = min(r.price for r in records)
    return records, f"{len(records)} record(s)  cheapest=${cheapest:,}"


def stage_storage(records):
    """Query DynamoDB for each flight's price history (read-only)."""
    history = PriceHistory(settings.dynamodb_table, region=settings.aws_region)

    history_count = 0
    for rec in records:
        last = history.last_price(rec.flight_id)
        if last:
            history_count += 1

    return f"queried {len(records)} flight(s)  {history_count} have history"


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Integration harness for Google Flights Price Tracker")
    parser.add_argument(
        "--stage",
        choices=["browser", "auth", "intercept", "prices", "storage"],
        help="Run a single stage instead of the full suite",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless (default: visible window)",
    )
    args = parser.parse_args()

    log.info("Starting integration harness  headless=%s  stage=%s", args.headless, args.stage or "all")

    driver = None
    try:
        nuke_chrome()
        driver = build_driver(settings) if args.headless else _build_visible_driver()
        state  = StateStore(settings.s3_bucket, region=settings.aws_region)

        # Shared state threaded through stages
        captured   = None
        page_html  = None
        records    = None

        all_stages = args.stage is None

        # ── Browser stage ──────────────────────────────────────────────────────
        if all_stages or args.stage == "browser":
            r = _run_stage("browser", lambda: stage_browser(driver))
            if not r.passed and not all_stages:
                sys.exit(1)

        # ── Auth stage ─────────────────────────────────────────────────────────
        if all_stages or args.stage == "auth":
            r = _run_stage("auth", lambda: stage_auth(driver, state))
            if not r.passed:
                if not all_stages:
                    sys.exit(1)
                # Auth failed — remaining stages can't proceed
                _print_summary()
                sys.exit(1)

        # ── Intercept stage ────────────────────────────────────────────────────
        if all_stages or args.stage == "intercept":
            def _intercept():
                nonlocal captured, page_html
                result = stage_intercept(driver)
                captured, page_html = result[0], result[1]
                return result[2]  # detail string

            r = _run_stage("intercept", _intercept)
            if not r.passed and not all_stages:
                sys.exit(1)

        # ── Prices stage ───────────────────────────────────────────────────────
        if all_stages or args.stage == "prices":
            if captured is None and args.stage == "prices":
                # Ran prices standalone — need to intercept first
                log.info("Running intercept stage to capture request for prices stage")
                captured, page_html, _ = stage_intercept(driver)

            def _prices():
                nonlocal records
                result = stage_prices(driver, captured, page_html)
                records = result[0]
                return result[1]

            r = _run_stage("prices", _prices)
            if not r.passed and not all_stages:
                sys.exit(1)

        # ── Storage stage ──────────────────────────────────────────────────────
        if all_stages or args.stage == "storage":
            if records is None and args.stage == "storage":
                log.warning("No price records from previous stage — storage stage will be limited")
                records = []

            if records is not None:
                r = _run_stage("storage", lambda: stage_storage(records))
            else:
                print(f"\n  {SKIP}  storage  (no records to query)")

        # ── Full results print ─────────────────────────────────────────────────
        if all_stages:
            _print_summary()

            # Detailed price table
            if records:
                print(f"\n{_BOLD}  Price Records{_RESET}")
                print(f"{'─' * 60}")
                for r in records:
                    print(f"  ${r.price:>6,}  {r.flight_id[:24]}...")
                print(f"{'─' * 60}\n")

            failed = [r for r in _results if not r.passed]
            sys.exit(1 if failed else 0)

    except KeyboardInterrupt:
        print("\n  Interrupted")
        sys.exit(130)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
