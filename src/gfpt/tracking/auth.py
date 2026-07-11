"""
Google authentication + session caching.

Session caching is the single biggest speed and stealth improvement:
  - After a successful login, all cookies are serialised to S3.
  - On the next invocation, we inject those cookies, navigate to the Flights
    saves page, and check whether Google still considers us authenticated.
  - If yes  → skip the entire login flow (~20-30 s saved per run, and far
    fewer password logins for Google's anomaly detection to notice).
  - If no   → do a full login and write fresh cookies back to S3.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pyotp
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from gfpt.config import (
    FLIGHTS_SAVES_URL,
    LOGIN_COOLDOWN_AFTER,
    LOGIN_RETRY_EVERY_N,
    SESSION_MAX_AGE_SECS,
    Settings,
)
from gfpt.storage.state import StateStore

if TYPE_CHECKING:
    from selenium import webdriver

log = logging.getLogger(__name__)


class LoginFailedError(RuntimeError):
    """Google refused the login (challenge, CAPTCHA, rejected credentials).

    Raised instead of limping on unauthenticated: the runner counts these
    separately so repeated failures back off instead of re-running a full
    password+TOTP login every 15 minutes against the shared account."""


# URL fragments that identify a Google login-challenge screen — these mean
# "Google wants a human", and retrying immediately makes the account look
# MORE suspicious, not less.
_CHALLENGE_MARKERS = (
    "/challenge/",
    "/signin/rejected",
    "captcha",
    "speedbump",
    "deniedsigninrejected",
)

# ── Selectors for Google's post-login interstitials ───────────────────────────
# Google shows passkey creation, phone backup, and recovery prompts after auth.
# Each selector is tried in order; first match wins.
_SKIP_BUTTON_SELECTORS = [
    # CSS — checked first (faster than XPath)
    (By.CSS_SELECTOR, "button[jsname='V67aGc']"),   # "Not now"  passkey v1
    (By.CSS_SELECTOR, "button[jsname='LgbsSe']"),   # "Skip"     some versions
    (By.CSS_SELECTOR, "[data-action='skip']"),
    # XPath text match — generic fallback for new prompt layouts
    (By.XPATH, "//button[normalize-space()='Not now']"),
    (By.XPATH, "//button[normalize-space()='Maybe later']"),
    (By.XPATH, "//button[normalize-space()='No thanks']"),
    (By.XPATH, "//button[normalize-space()='Skip']"),
    (By.XPATH, "//span[normalize-space()='Not now']/ancestor::button[1]"),
    (By.XPATH, "//span[normalize-space()='Maybe later']/ancestor::button[1]"),
    (By.XPATH, "//span[normalize-space()='No thanks']/ancestor::button[1]"),
]


# ── Public API ─────────────────────────────────────────────────────────────────

def ensure_logged_in(driver: webdriver.Chrome, settings: Settings,
                     state: StateStore) -> None:
    """
    Guarantee the driver is authenticated before we start intercepting.

    Strategy:
      1. Download cookies from S3 and inject them.
      2. Navigate to the saves page — if still authenticated, done.
      3. Otherwise, run the full login flow and persist new cookies to S3.
    """
    if _try_restore_session(driver, state):
        log.info("Session restored from S3 — skipping login")
        # Dismiss any prompts that appear even on a restored session
        _try_dismiss_prompt(driver)
        return

    _check_login_cooldown(state)

    log.info("No valid session — performing full login")
    do_login(driver, settings, state)

    # Verify on the actual target page: an authenticated session stays on
    # saves, an unauthenticated one gets redirected back to accounts.
    driver.get(FLIGHTS_SAVES_URL)
    if not is_authenticated(driver):
        _snapshot(driver, state, "login-not-authenticated")
        raise LoginFailedError(
            f"login flow completed but Google still refuses the session "
            f"(landed on {driver.current_url[:80]})"
        )

    # Only a VERIFIED session is worth caching — persisting unauthenticated
    # cookies would poison every subsequent run's session restore.
    save_session(driver, state)


def _check_login_cooldown(state: StateStore) -> None:
    """
    Back off after repeated login failures. The first few failures retry
    every run (fast recovery from blips); after LOGIN_COOLDOWN_AFTER, only
    every LOGIN_RETRY_EVERY_N-th run attempts a real login — the rest fail
    fast without touching Google (the run still counts as failed).
    """
    failures = 0
    try:
        failures = int(state.load_health().get("login_failures", 0))
    except (TypeError, ValueError):
        pass
    if failures >= LOGIN_COOLDOWN_AFTER and failures % LOGIN_RETRY_EVERY_N != 0:
        raise LoginFailedError(
            f"login cooldown: {failures} consecutive login failures — "
            f"next real attempt in "
            f"{LOGIN_RETRY_EVERY_N - failures % LOGIN_RETRY_EVERY_N} run(s)"
        )


def save_session(driver: webdriver.Chrome, state: StateStore) -> None:
    """Serialise all driver cookies to S3 for the next invocation."""
    payload = {
        "saved_at": datetime.now(UTC).isoformat(),
        "cookies": driver.get_cookies(),
    }
    if state.save_session(payload):
        log.info("Session cookies saved to S3")


def is_authenticated(driver: webdriver.Chrome) -> bool:
    """
    Return True if we are on the Flights saves page (not redirected to login).
    Google redirects unauthenticated users to accounts.google.com.
    """
    url = driver.current_url
    if "accounts.google.com" in url or "myaccount.google.com" in url:
        return False
    # Extra check: look for the sign-in call-to-action
    try:
        driver.find_element(By.CSS_SELECTOR, "a[href*='ServiceLogin']")
        return False  # sign-in link is visible
    except NoSuchElementException:
        return True


# ── Session restore ────────────────────────────────────────────────────────────

def _try_restore_session(driver: webdriver.Chrome, state: StateStore) -> bool:
    """
    Load cookies from S3, inject them, navigate to saves, return True if
    Google accepts the session.
    """
    data = state.load_session()
    if not data:
        log.info("No session file in S3")
        return False

    # Check session age
    try:
        saved_at = datetime.fromisoformat(data["saved_at"])
        age = (datetime.now(UTC) - saved_at).total_seconds()
        if age > SESSION_MAX_AGE_SECS:
            log.info("Cached session is %.0f h old — forcing fresh login", age / 3600)
            return False
    except (KeyError, TypeError, ValueError):
        pass

    _inject_cookies(driver, data.get("cookies", []))

    # Navigate to saves page and check authentication status.
    try:
        driver.get(FLIGHTS_SAVES_URL)
        if is_authenticated(driver):
            return True
        log.info("Session cookies injected but Google re-auth required")
        return False
    except Exception as exc:
        log.warning("Session check navigation failed: %s", exc)
        return False


def _inject_cookies(driver: webdriver.Chrome, cookies: list[dict]) -> None:
    """
    Inject cookies via CDP Network.setCookie — no prior domain navigation
    needed, unlike Selenium's add_cookie which requires being on the target
    domain first.
    """
    for cookie in cookies:
        try:
            cdp_cookie = {
                "name":     cookie["name"],
                "value":    cookie["value"],
                "domain":   cookie.get("domain", ".google.com"),
                "path":     cookie.get("path", "/"),
                "secure":   bool(cookie.get("secure", False)),
                "httpOnly": bool(cookie.get("httpOnly", False)),
            }
            if cookie.get("expiry"):
                cdp_cookie["expires"] = int(cookie["expiry"])
            if cookie.get("sameSite") in ("None", "Lax", "Strict"):
                cdp_cookie["sameSite"] = cookie["sameSite"]
            driver.execute_cdp_cmd("Network.setCookie", cdp_cookie)
        except Exception as exc:
            log.debug("Skipped cookie %s: %s", cookie.get("name", "?"), exc)


# ── Full login flow ────────────────────────────────────────────────────────────

def do_login(driver: webdriver.Chrome, settings: Settings,
             state: StateStore) -> None:
    """
    Drive through Google's email → password → (TOTP) → (interstitials) flow.

    Google can present any of these screens after password:
      - TOTP / authenticator code
      - Passkey creation prompt  →  dismissed via _try_dismiss_prompt
      - Phone/recovery prompt    →  dismissed via _try_dismiss_prompt
      - Destination page         →  done

    Slow, human-paced typing is intentional — it avoids rate-limit triggers.
    """
    wait = WebDriverWait(driver, 30)

    # ── Navigate to saves (Google will redirect to login) ─────────────────────
    driver.get(FLIGHTS_SAVES_URL)
    log.info("Login: landed on %s", driver.current_url[:80])

    # ── Click Sign in button (page may not auto-redirect) ──────────────────────
    # Google Flights saves page shows a "Sign in" button instead of redirecting
    # directly to accounts.google.com when the session has no cookies at all.
    if "accounts.google.com" not in driver.current_url:
        log.info("Login: not yet on accounts page — looking for Sign in button")
        try:
            sign_in = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH,
                    "//a[contains(@href,'accounts.google.com')] | "
                    "//a[normalize-space()='Sign in'] | "
                    "//button[normalize-space()='Sign in']"
                ))
            )
            sign_in.click()
            time.sleep(1.5)
            log.info("Login: after sign-in click, url=%s", driver.current_url[:80])
        except TimeoutException:
            _snapshot(driver, state, "login-signin-button-timeout")
            log.warning("Login: Sign in button not found — may have auto-redirected")

    # ── Email ──────────────────────────────────────────────────────────────────
    log.info("Login: waiting for email field")
    try:
        email_field = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='identifier']"))
        )
    except TimeoutException:
        _snapshot(driver, state, "login-email-timeout")
        raise
    _slow_type(email_field, settings.google_email)
    email_field.send_keys(Keys.RETURN)
    time.sleep(1.5)

    # ── Password ───────────────────────────────────────────────────────────────
    log.info("Login: waiting for password field  url=%s", driver.current_url[:80])
    try:
        pwd_field = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='Passwd']"))
        )
    except TimeoutException:
        _snapshot(driver, state, "login-password-timeout")
        raise
    _slow_type(pwd_field, settings.google_password)
    pwd_field.send_keys(Keys.RETURN)
    time.sleep(1.5)

    # Dismiss any passkey / backup prompts that appear before or instead of TOTP
    _try_dismiss_prompt(driver)

    # ── TOTP (may or may not appear) ───────────────────────────────────────────
    try:
        totp_field = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='totpPin']"))
        )
        log.info("Login: entering TOTP")
        code = pyotp.TOTP(settings.totp_secret).now()
        _slow_type(totp_field, code, delay=0.12)
        totp_field.send_keys(Keys.RETURN)
        time.sleep(2)
    except TimeoutException:
        log.info("Login: TOTP screen not shown (trusted device or dismissed)")

    # Dismiss any passkey / recovery prompts shown after TOTP
    _try_dismiss_prompt(driver)

    # ── Verify success ─────────────────────────────────────────────────────────
    try:
        WebDriverWait(driver, 15).until(
            lambda d: "accounts.google.com" not in d.current_url
        )
        log.info("Login: success (redirected to %s)", driver.current_url[:60])
    except TimeoutException:
        url = driver.current_url
        _snapshot(driver, state, "login-stuck-on-accounts")
        marker = next((m for m in _CHALLENGE_MARKERS if m in url.lower()), None)
        if marker:
            raise LoginFailedError(
                f"Google presented a verification challenge ({marker}) — "
                "manual sign-in from a trusted device may be needed"
            ) from None
        raise LoginFailedError(
            f"still on accounts.google.com after login flow ({url[:80]})"
        ) from None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _try_dismiss_prompt(driver: webdriver.Chrome) -> bool:
    """
    Dismiss any Google interstitial shown after authentication:
    passkey creation, phone backup, 'protect your account', etc.

    Only acts while still on accounts.google.com. Returns True if a
    prompt was found and dismissed.
    """
    if "accounts.google.com" not in driver.current_url:
        return False

    for by, selector in _SKIP_BUTTON_SELECTORS:
        try:
            btn = WebDriverWait(driver, 0.5).until(
                EC.element_to_be_clickable((by, selector))
            )
            btn.click()
            log.info("Dismissed Google interstitial (%s: %s)", by, selector)
            time.sleep(0.5)
            return True
        except Exception:
            continue

    return False


def _slow_type(element, text: str, delay: float = 0.07) -> None:
    """Type one character at a time to mimic human input pacing."""
    for char in text:
        element.send_keys(char)
        time.sleep(delay)


def _snapshot(driver: webdriver.Chrome, state: StateStore, label: str) -> None:
    """Upload a screenshot to S3 for post-mortem debugging. Never raises."""
    log.warning("Login stalled — url=%s", driver.current_url)
    try:
        key = state.save_screenshot(f"auth-{label}", driver.get_screenshot_as_png())
        if key:
            log.warning("Screenshot saved → s3://%s/%s", state.bucket, key)
    except Exception as exc:
        log.warning("Could not save screenshot: %s", exc)
