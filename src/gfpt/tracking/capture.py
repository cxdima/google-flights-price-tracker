"""
CDP network interception — capture the GetSolutionPrices POSTs that the
Google Flights saves page fires as it renders.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from gfpt.config import FLIGHTS_SAVES_URL

if TYPE_CHECKING:
    from selenium import webdriver

log = logging.getLogger(__name__)

PRICES_ENDPOINT = "travel.frontend.flights.FlightsFrontendService/GetSolutionPrices"

# Exit after this many seconds without a new GetSolutionPrices request —
# gives Google Flights enough time to fire a new batch after each scroll
# step without us exiting too early.
_SILENCE_SECS = 2.5

# On a healthy page the FIRST request appears within seconds; a page that
# has produced nothing after this long is dead, and polling out the full
# hydrate window would burn billed seconds and — worse — eat into the
# Lambda-timeout headroom that failure recording depends on.
_FIRST_CAPTURE_SECS = 25

# Scroll positions — Google Flights only fires GetSolutionPrices for rows
# visible in the viewport. Large values are harmless on shorter pages.
_SCROLL_POSITIONS = [600, 1400, 2500, 4000, 6000, 9000, 14000, 20000]
_SCROLL_EVERY_SECS = 0.5


def intercept_api_call(
    driver: webdriver.Chrome,
    hydrate_secs: int,
    skip_nav: bool = False,
) -> tuple[list[dict], str]:
    """
    Poll CDP performance logs until all GetSolutionPrices POSTs are captured.

    skip_nav=True  — caller already navigated to the saves page and CDP was
                     enabled before that navigation; skip the redundant reload.
    skip_nav=False — navigate now (first run or after a full login).

    Returns (list_of_unique_requests, page_html).
    Raises RuntimeError if no call is observed after all attempts.
    """
    driver.execute_cdp_cmd("Network.enable", {})

    if skip_nav:
        log.info("Skipping re-navigation — already on saves page (warm path)")
    else:
        log.info("Navigating to saves page — waiting up to %ds for API call",
                 hydrate_secs)
        driver.get(FLIGHTS_SAVES_URL)

    captured = _poll_for_prices(driver, hydrate_secs)

    # Warm-path fallback: if skip_nav captured nothing, the CDP logs were
    # likely consumed during session restore. Force a page reload — but with
    # a capped window: two full hydrate polls plus login waits would push a
    # failing run past the Lambda timeout, where the failure-recording path
    # can't run.
    if not captured and skip_nav:
        log.warning("Warm path captured nothing — reloading page")
        try:
            driver.get_log("performance")  # drain stale entries
        except Exception:
            pass
        driver.get(FLIGHTS_SAVES_URL)
        captured = _poll_for_prices(driver, min(hydrate_secs, _FIRST_CAPTURE_SECS))

    if not captured:
        raise RuntimeError(
            f"GetSolutionPrices was not observed within {hydrate_secs}s"
        )

    log.info("Captured %d unique GetSolutionPrices request(s)", len(captured))
    return captured, driver.page_source


def _poll_for_prices(driver: webdriver.Chrome, hydrate_secs: int) -> list[dict]:
    """
    Poll CDP performance logs for GetSolutionPrices POSTs, scrolling the
    page to trigger lazy-loaded flight cards.

    Returns a list of unique captured requests (may be empty).
    """
    captured: list[dict] = []
    seen_post_data: set[str] = set()
    last_found_at: float | None = None
    start = time.monotonic()
    deadline = start + hydrate_secs
    first_capture_deadline = start + min(hydrate_secs, _FIRST_CAPTURE_SECS)

    scroll_idx = 0
    last_scroll_t = start

    while time.monotonic() < deadline:
        # Dead page: nothing at all captured in the first-capture window.
        if not captured and time.monotonic() >= first_capture_deadline:
            log.warning("No request captured within %.0fs — aborting poll early",
                        time.monotonic() - start)
            break
        found_new = False
        for entry in driver.get_log("performance"):
            try:
                msg = json.loads(entry["message"])["message"]
            except (KeyError, TypeError, ValueError):
                continue

            if msg.get("method") != "Network.requestWillBeSent":
                continue

            req = msg.get("params", {}).get("request", {})
            if PRICES_ENDPOINT not in req.get("url", ""):
                continue

            # Deduplicate: the SPA may re-send the same request on retry.
            key = req.get("postData", "")
            if key in seen_post_data:
                continue
            seen_post_data.add(key)
            captured.append(req)
            found_new = True
            log.info("Captured GetSolutionPrices request #%d", len(captured))

        if found_new:
            last_found_at = time.monotonic()

        # Scroll down periodically to trigger lazy-loaded flight rows.
        if scroll_idx < len(_SCROLL_POSITIONS):
            if time.monotonic() - last_scroll_t >= _SCROLL_EVERY_SECS:
                pos = _SCROLL_POSITIONS[scroll_idx]
                try:
                    driver.execute_script(f"window.scrollTo(0, {pos});")
                except Exception:
                    pass
                scroll_idx += 1
                last_scroll_t = time.monotonic()

        # Exit only after silence — ensures all progressive batches are caught.
        if captured and last_found_at and (time.monotonic() - last_found_at) >= _SILENCE_SECS:
            break

        time.sleep(0.15)

    return captured


def build_session_headers(driver: webdriver.Chrome) -> dict:
    """
    Extract cookies and User-Agent from the Selenium session once so the
    captured requests can be re-fired from plain Python after Chrome exits.
    """
    cookies_str = "; ".join(
        f"{c['name']}={c['value']}" for c in driver.get_cookies()
    )
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookies_str,
        "X-Same-Domain": "1",
        "Origin": "https://www.google.com",
        "Referer": FLIGHTS_SAVES_URL,
        "User-Agent": driver.execute_script("return navigator.userAgent"),
    }
