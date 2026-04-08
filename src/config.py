"""
Centralised configuration — all env vars and constants in one place.
"""
import os
from zoneinfo import ZoneInfo

# ── Google Account ────────────────────────────────────────────────────────────
GOOGLE_EMAIL    = os.environ.get("GOOGLE_EMAIL", "")
GOOGLE_PASSWORD = os.environ.get("GOOGLE_PASSWORD", "")
TOTP_SECRET     = os.environ.get("TOTP_SECRET", "")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS  = [
    c.strip()
    for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",")
    if c.strip()
]

# ── AWS ───────────────────────────────────────────────────────────────────────
S3_BUCKET      = os.environ.get("S3_BUCKET", "")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "gfpricetracker-prices")
AWS_REGION     = os.environ.get("AWS_REGION", "us-east-1")

# ── Chrome ────────────────────────────────────────────────────────────────────
CHROME_BINARY      = os.environ.get("CHROME_BINARY", "/usr/bin/chromium")
CHROMEDRIVER_PATH  = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

# User-agent: current stable Windows Chrome — blends into real traffic
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# ── Execution ─────────────────────────────────────────────────────────────────
# Seconds to wait for the GetSolutionPrices network call after page load.
# With 8 scroll positions × 0.8 s each plus 3 s silence window, we need
# at least ~10 s just for the scroll phase.  45 s gives ample headroom
# and accommodates slow Lambda cold starts.
HYDRATE_SECS = int(os.environ.get("HYDRATE_SECS", "45"))

# S3 key where the session cookie file is stored
SESSION_S3_KEY = "sessions/latest.json"

# Maximum age of a saved session before we force a fresh login (6 hours)
SESSION_MAX_AGE_SECS = 6 * 60 * 60

# ── URLs ──────────────────────────────────────────────────────────────────────
FLIGHTS_SAVES_URL = "https://www.google.com/travel/flights/saves"
GOOGLE_HOME_URL   = "https://www.google.com"

# ── Misc ──────────────────────────────────────────────────────────────────────
TZ = ZoneInfo("America/Chicago")
