"""
Google authentication + session caching.

Session caching is the single biggest speed improvement:
  - After a successful login, all cookies are serialised to S3.
  - On the next invocation, we load those cookies, navigate to the Flights
    saves page, and check whether Google still considers us authenticated.
  - If yes  → skip the entire login flow (~20-30 s saved per run).
  - If no   → do a full login and write fresh cookies back to S3.
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pyotp
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from config import (
    GOOGLE_EMAIL, GOOGLE_PASSWORD, TOTP_SECRET,
    S3_BUCKET, SESSION_S3_KEY, SESSION_MAX_AGE_SECS,
    FLIGHTS_SAVES_URL,
)

if TYPE_CHECKING:
    from selenium import webdriver

log = logging.getLogger(__name__)

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

def ensure_logged_in(driver: "webdriver.Chrome", s3) -> bool:
    """
    Guarantee the driver is authenticated before we start intercepting.

    Strategy:
      1. Download cookies from S3 and inject them.
      2. Navigate to the saves page — if still authenticated, return True.
      3. Otherwise, run the full login flow and persist new cookies to S3.
    """
    if _try_restore_session(driver, s3):
        log.info("Session restored from S3 — skipping login")
        # Dismiss any prompts that appear even on a restored session
        _try_dismiss_prompt(driver)
        return True

    log.info("No valid session — performing full login")
    do_login(driver)
    _save_session(driver, s3)
    return True


# ── Post-login prompt dismissal ────────────────────────────────────────────────

def _try_dismiss_prompt(driver: "webdriver.Chrome") -> bool:
    """
    Dismiss any Google interstitial shown after authentication:
    passkey creation, phone backup, 'protect your account', etc.

    Only acts while still on accounts.google.com. Returns True if a
    prompt was found and dismissed.
    """
    if "accounts.google.com" not in driver.current_url:
        return False

    # Try CSS selectors first (fast) with a very short timeout,
    # then fall back to XPath text matches.
    for by, selector in _SKIP_BUTTON_SELECTORS:
        try:
            btn = WebDriverWait(driver, 0.5).until(
                EC.element_to_be_clickable((by, selector))
            )
            btn.click()
            log.info("Dismissed Google interstitial (%s: %s)", by, selector)
            time.sleep(0.5)
            return True
        except (TimeoutException, NoSuchElementException, Exception):
            continue

    return False


# ── Session persistence ────────────────────────────────────────────────────────

def _try_restore_session(driver: "webdriver.Chrome", s3) -> bool:
    """
    Load cookies from S3, inject them, navigate to saves, return True if
    Google accepts the session.
    """
    if not S3_BUCKET:
        return False

    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=SESSION_S3_KEY)
        data = json.loads(obj["Body"].read().decode())
    except Exception as exc:
        log.info("No session file in S3 (%s)", exc)
        return False

    # Check session age
    try:
        saved_at = datetime.fromisoformat(data["saved_at"])
        age = (datetime.now(timezone.utc) - saved_at).total_seconds()
        if age > SESSION_MAX_AGE_SECS:
            log.info("Cached session is %.0f h old — forcing fresh login", age / 3600)
            return False
    except Exception:
        pass

    # Inject cookies via CDP Network.setCookie — no prior domain navigation needed,
    # unlike Selenium's add_cookie which requires being on the target domain first.
    try:
        for cookie in data.get("cookies", []):
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
            except Exception:
                pass
    except Exception as exc:
        log.warning("Failed to inject cookies via CDP: %s", exc)
        return False

    # Navigate to saves page and check authentication status.
    try:
        driver.get(FLIGHTS_SAVES_URL)
        if _is_authenticated(driver):
            return True
        log.info("Session cookies injected but Google re-auth required")
        return False
    except Exception as exc:
        log.warning("Session check navigation failed: %s", exc)
        return False


def _save_session(driver: "webdriver.Chrome", s3) -> None:
    """Serialise all driver cookies to S3 for the next invocation."""
    if not S3_BUCKET:
        return
    try:
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "cookies":  driver.get_cookies(),
        }
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=SESSION_S3_KEY,
            Body=json.dumps(payload).encode(),
            ContentType="application/json",
        )
        log.info("Session cookies saved to S3")
    except Exception as exc:
        log.warning("Could not save session to S3: %s", exc)


def _is_authenticated(driver: "webdriver.Chrome") -> bool:
    """
    Return True if we are on the Flights saves page (not redirected to login).
    Google redirects unauthenticated users to accounts.google.com.
    """
    url = driver.current_url
    if "accounts.google.com" in url:
        return False
    if "myaccount.google.com" in url:
        return False
    # Extra check: look for the sign-in call-to-action
    try:
        driver.find_element(By.CSS_SELECTOR, "a[href*='ServiceLogin']")
        return False  # sign-in link is visible
    except NoSuchElementException:
        pass
    return True


# ── Full login flow ────────────────────────────────────────────────────────────

def do_login(driver: "webdriver.Chrome") -> None:
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
            _snapshot(driver, "login-signin-button-timeout")
            log.warning("Login: Sign in button not found — may have auto-redirected or page changed")

    # ── Email ──────────────────────────────────────────────────────────────────
    log.info("Login: waiting for email field")
    try:
        email_field = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='identifier']"))
        )
    except TimeoutException:
        _snapshot(driver, "login-email-timeout")
        raise
    _slow_type(email_field, GOOGLE_EMAIL)
    email_field.send_keys(Keys.RETURN)
    time.sleep(1.5)

    # ── Password ───────────────────────────────────────────────────────────────
    log.info("Login: waiting for password field  url=%s", driver.current_url[:80])
    try:
        pwd_field = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='Passwd']"))
        )
    except TimeoutException:
        _snapshot(driver, "login-password-timeout")
        raise
    _slow_type(pwd_field, GOOGLE_PASSWORD)
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
        code = pyotp.TOTP(TOTP_SECRET).now()
        _slow_type(totp_field, code, delay=0.12)
        totp_field.send_keys(Keys.RETURN)
        time.sleep(2)
    except TimeoutException:
        log.info("Login: TOTP screen not shown (trusted device, skipped, or prompt dismissed)")

    # Dismiss any passkey / recovery prompts shown after TOTP
    _try_dismiss_prompt(driver)

    # ── Verify success ─────────────────────────────────────────────────────────
    try:
        WebDriverWait(driver, 15).until(
            lambda d: "accounts.google.com" not in d.current_url
        )
        log.info("Login: success (redirected to %s)", driver.current_url[:60])
    except TimeoutException:
        log.warning("Login: still on accounts page after waiting — continuing anyway")


def _slow_type(element, text: str, delay: float = 0.07) -> None:
    """Type one character at a time to mimic human input pacing."""
    for char in text:
        element.send_keys(char)
        time.sleep(delay)


def _snapshot(driver: "webdriver.Chrome", label: str) -> None:
    """
    Upload a screenshot + current URL to S3 for post-mortem debugging.
    Logs the S3 path (or a warning if S3 isn't configured) and never raises.
    """
    import boto3
    log.warning("Login stalled — url=%s", driver.current_url)
    if not S3_BUCKET:
        return
    try:
        key = f"screenshots/auth-{label}-{int(time.time())}.png"
        boto3.client("s3").put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=driver.get_screenshot_as_png(),
            ContentType="image/png",
        )
        log.warning("Screenshot saved → s3://%s/%s", S3_BUCKET, key)
    except Exception as exc:
        log.warning("Could not save screenshot: %s", exc)
