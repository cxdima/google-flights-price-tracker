"""
pytest configuration — env defaults and shared fixtures.

`pyproject.toml` sets pythonpath=["src"], so `import gfpt.x` works directly;
no sys.path bootstrap is needed here.

Unit tests must never depend on real credentials: the .env file is NOT
loaded. Every test runs against the deterministic env below (autouse
fixture), and the @lru_cache on gfpt.config.load_settings is cleared
before and after each test so no Settings object leaks between tests.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import gfpt.config

# ── Shared constants ───────────────────────────────────────────────────────────

FLIGHT_ID_1 = "id_aaaa1111bbbb2222cccc3333dddd4444"   # id_ + 32 lowercase hex
FLIGHT_ID_2 = "id_bbbb2222cccc3333dddd4444eeee5555"   # id_ + 32 lowercase hex
FLIGHT_ID_SHORT = "TOOSHORT"                           # must be rejected
SEARCH_URL_1 = "https://www.google.com/travel/flights?tfs=abc123"

CHAT_ID_DMITRY = "12345"
CHAT_ID_ALEX = "67890"

_UNIT_TEST_ENV = {
    "GOOGLE_EMAIL":       "test@example.com",
    "GOOGLE_PASSWORD":    "test-password",
    "TOTP_SECRET":        "JBSWY3DPEHPK3PXP",   # well-known test Base32 key
    "TELEGRAM_BOT_TOKEN": "0:test-token",
    "TELEGRAM_USERS":     f"{CHAT_ID_DMITRY}:Dmitry,{CHAT_ID_ALEX}:Alex",
    "S3_BUCKET":          "test-bucket",
    "DYNAMODB_TABLE":     "test-prices",
    "AWS_REGION":         "us-east-1",
}


@pytest.fixture(autouse=True)
def default_env(monkeypatch):
    """Deterministic settings for every test; cache cleared on both sides."""
    for key, value in _UNIT_TEST_ENV.items():
        monkeypatch.setenv(key, value)
    # Never let a developer's real legacy var leak into tests.
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    gfpt.config.load_settings.cache_clear()
    yield
    gfpt.config.load_settings.cache_clear()


# ── Fake state store (dict-backed, no AWS) ─────────────────────────────────────

class FakeStateStore:
    """In-memory stand-in for gfpt.storage.state.StateStore."""

    def __init__(self):
        self.prefs: dict = {}
        self.manifest: dict = {}
        self.summary: dict | None = None
        self.session: dict | None = None
        self.streak: int = 0

    def load_user_prefs(self) -> dict:
        return dict(self.prefs)

    def save_user_prefs(self, prefs: dict) -> bool:
        self.prefs = dict(prefs)
        return True

    def load_manifest(self) -> dict:
        return dict(self.manifest)

    def save_manifest(self, manifest: dict) -> bool:
        self.manifest = dict(manifest)
        return True

    def load_summary(self) -> dict | None:
        return self.summary

    def save_summary(self, summary: dict) -> bool:
        self.summary = dict(summary)
        return True

    def load_session(self) -> dict | None:
        return self.session

    def save_session(self, payload: dict) -> bool:
        self.session = dict(payload)
        return True

    def load_failure_streak(self) -> int:
        return self.streak

    def save_failure_streak(self, count: int) -> bool:
        self.streak = count
        return True


@pytest.fixture
def fake_state():
    return FakeStateStore()


# ── Price response fixtures ────────────────────────────────────────────────────

@pytest.fixture
def sample_price_body():
    """
    Realistic fake GetSolutionPrices response.

    Flight 1: price=$450, has a search URL.
    Flight 2: price=$820, no URL.
    (Trailing numbers like 500 in flight 1's row mimic the noise the real
    response carries — the parser must pick the FIRST plausible price.)
    """
    inner = [
        [FLIGHT_ID_1, 450, 500, SEARCH_URL_1],
        [FLIGHT_ID_2, 820],
    ]
    frame = json.dumps(inner)
    outer = [["wrb.fr", "GetSolutionPrices", frame, None, None]]
    return b")]}'\n" + json.dumps(outer).encode()


@pytest.fixture
def sample_price_body_no_xsrf():
    """Same shape but without the XSRF safety prefix."""
    inner = [[FLIGHT_ID_1, 300]]
    frame = json.dumps(inner)
    outer = [["wrb.fr", "GetSolutionPrices", frame, None, None]]
    return json.dumps(outer).encode()


@pytest.fixture
def sample_captured_request():
    """Fake CDP-captured GetSolutionPrices POST request dict."""
    import urllib.parse

    f_req = json.dumps([None, [FLIGHT_ID_1], 0])
    post_data = urllib.parse.urlencode({"f.req": f_req, "at": "test_at_token"})
    return {
        "url": (
            "https://www.google.com/_/FlightsFrontendUi/data/"
            "travel.frontend.flights.FlightsFrontendService/GetSolutionPrices"
            "?f.sid=SID123&bl=boq_test&hl=en-US&gl=US"
            "&soc-app=162&soc-platform=1&soc-device=4&_reqid=142865&rt=c"
        ),
        "postData": post_data,
    }


# ── Saves-page HTML fixtures ───────────────────────────────────────────────────

def _segment_row(airline, flight_num, origin, dep_date, dep_time,
                 dest, arr_date, arr_time, duration, via=None):
    """One journey slice in the positional array format the page uses."""
    return [
        None,           # [0]
        [airline],      # [1] airline
        [flight_num],   # [2] flight numbers  [["UA", "100"]]
        origin,         # [3] origin IATA
        dep_date,       # [4] departure date  [y, m, d]
        dep_time,       # [5] departure time  [h, m]
        dest,           # [6] destination IATA
        arr_date,       # [7] arrival date
        arr_time,       # [8] arrival time
        duration,       # [9] duration minutes
        None,           # [10] padding
        via,            # [11] stopover airports, or None if direct
    ]


def _page_html_for(flight_id: str, segments: list) -> str:
    details = [
        None,                                # [0]
        "/travel/flights?tfs=abc123",        # [1] search URL
        None,                                # [2]
        None,                                # [3]
        [segments, ["United Airlines"]],     # [4] [[segment_row, ...], [airline]]
    ]
    data_array = [None, [[flight_id, details]]]
    block = json.dumps({"key": "ds:1", "data": data_array})
    return (
        "<html><body><script>"
        f"AF_initDataCallback({block});"
        "</script></body></html>"
    )


@pytest.fixture
def sample_page_html():
    """Saves page with one direct one-way flight: ORD → LAX."""
    segment = _segment_row(
        "United Airlines", ["UA", "100"],
        "ORD", [2026, 4, 1], [8, 30],
        "LAX", [2026, 4, 1], [11, 45], 195,
    )
    return _page_html_for(FLIGHT_ID_1, [segment])


@pytest.fixture
def round_trip_page_html():
    """Saves page with one round trip: ORD → LAX outbound, LAX → ORD return."""
    outbound = _segment_row(
        "United Airlines", ["UA", "100"],
        "ORD", [2026, 4, 1], [8, 30],
        "LAX", [2026, 4, 1], [11, 45], 195,
    )
    inbound = _segment_row(
        "United Airlines", ["UA", "205"],
        "LAX", [2026, 4, 8], [14, 0],
        "ORD", [2026, 4, 8], [20, 15], 255,
    )
    return _page_html_for(FLIGHT_ID_1, [outbound, inbound])


# ── AWS mocks ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_s3_empty():
    """S3 client that raises NoSuchKey for every get_object call."""
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "GetObject",
    )
    return s3


@pytest.fixture
def mock_dynamodb_table():
    """DynamoDB Table mock that reports no existing price history."""
    table = MagicMock()
    table.query.return_value = {"Items": []}
    return table


@pytest.fixture
def mock_dynamodb_resource(mock_dynamodb_table):
    """DynamoDB resource whose .Table() returns the mock table."""
    resource = MagicMock()
    resource.Table.return_value = mock_dynamodb_table
    return resource
