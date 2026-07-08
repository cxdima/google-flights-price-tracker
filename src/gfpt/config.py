"""
Centralised configuration.

All environment access happens in `load_settings()` so nothing reads the
environment at import time (tests can set env vars and call
`load_settings.cache_clear()`).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

from gfpt.models import TelegramUser

log = logging.getLogger(__name__)

# ── Constants (not environment-dependent) ─────────────────────────────────────

FLIGHTS_SAVES_URL = "https://www.google.com/travel/flights/saves"

# User-agent: current stable Windows Chrome — blends into real traffic
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# Maximum age of a cached Google session before we force a fresh login
SESSION_MAX_AGE_SECS = 6 * 60 * 60

# A flight must be absent from this many consecutive runs before it is
# pruned from the manifest — protects against one bad scrape wiping the list.
MISSING_RUNS_BEFORE_REMOVAL = 2

# Notify users after this many consecutive failed runs (not on every failure)
FAILURE_NOTIFY_THRESHOLD = 3

TZ = ZoneInfo("America/Chicago")


# ── Settings ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    # Google account
    google_email: str
    google_password: str
    totp_secret: str
    # Telegram
    telegram_bot_token: str
    telegram_users: tuple[TelegramUser, ...]
    # AWS
    s3_bucket: str
    dynamodb_table: str
    aws_region: str
    # Chrome
    chrome_binary: str
    chromedriver_path: str
    # Seconds to wait for GetSolutionPrices network calls after page load
    hydrate_secs: int


def parse_telegram_users(users_raw: str, chat_ids_raw: str) -> tuple[TelegramUser, ...]:
    """
    Parse the authorized-user list.

    Preferred format (TELEGRAM_USERS):   "123456789:Dmitry,987654321:Alex"
    Legacy fallback (TELEGRAM_CHAT_ID):  "123456789,987654321"

    Entries without a name fall back to "User <first 4 digits>".
    """
    users: list[TelegramUser] = []
    seen: set[str] = set()

    source = users_raw if users_raw.strip() else chat_ids_raw
    for entry in source.split(","):
        entry = entry.strip()
        if not entry:
            continue
        chat_id, _, name = entry.partition(":")
        chat_id = chat_id.strip()
        name = name.strip()
        if not chat_id.lstrip("-").isdigit():
            log.warning("Ignoring malformed Telegram user entry: %r", entry)
            continue
        if chat_id in seen:
            continue
        seen.add(chat_id)
        users.append(TelegramUser(chat_id=chat_id, name=name or f"User {chat_id[:4]}"))

    return tuple(users)


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        google_email=env("GOOGLE_EMAIL", ""),
        google_password=env("GOOGLE_PASSWORD", ""),
        totp_secret=env("TOTP_SECRET", ""),
        telegram_bot_token=env("TELEGRAM_BOT_TOKEN", ""),
        telegram_users=parse_telegram_users(
            env("TELEGRAM_USERS", ""), env("TELEGRAM_CHAT_ID", "")
        ),
        s3_bucket=env("S3_BUCKET", ""),
        dynamodb_table=env("DYNAMODB_TABLE", "gfpricetracker-prices"),
        aws_region=env("AWS_REGION", "us-east-1"),
        chrome_binary=env("CHROME_BINARY", "/usr/bin/chromium"),
        chromedriver_path=env("CHROMEDRIVER_PATH", "/usr/bin/chromedriver"),
        hydrate_secs=int(env("HYDRATE_SECS", "45")),
    )
