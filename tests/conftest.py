"""
pytest configuration — path bootstrap, env setup, and shared fixtures.

Loaded automatically by pytest before any test file is collected.
"""
import json
import os
import sys
import urllib.parse
from unittest.mock import MagicMock

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
# Must happen before any project module is imported.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ── Load .env (real credentials, used by integration tests) ───────────────────
_env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

# ── Minimal stubs so unit tests work even without a .env ──────────────────────
_UNIT_TEST_DEFAULTS = {
    "GOOGLE_EMAIL":        "test@example.com",
    "GOOGLE_PASSWORD":     "test-password",
    "TOTP_SECRET":         "JBSWY3DPEHPK3PXP",   # well-known test Base32 key
    "TELEGRAM_BOT_TOKEN":  "0:test-token",
    "TELEGRAM_CHAT_ID":    "12345",
    "S3_BUCKET":           "test-bucket",
    "DYNAMODB_TABLE":      "test-prices",
    "AWS_REGION":          "us-east-1",
}
for _k, _v in _UNIT_TEST_DEFAULTS.items():
    os.environ.setdefault(_k, _v)


# ── Shared constants ───────────────────────────────────────────────────────────

FLIGHT_ID_1 = "id_aaaa1111bbbb2222cccc3333dddd4444"   # id_ + 32 lowercase hex
FLIGHT_ID_2 = "id_bbbb2222cccc3333dddd4444eeee5555"   # id_ + 32 lowercase hex
FLIGHT_ID_SHORT = "TOOSHORT"                           # must be rejected
SEARCH_URL_1 = "https://www.google.com/travel/flights?tfs=abc123"


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def flight_id_1():
    return FLIGHT_ID_1


@pytest.fixture
def flight_id_2():
    return FLIGHT_ID_2


@pytest.fixture
def sample_price_body():
    """
    Realistic fake GetSolutionPrices response.

    Flight 1: price=$450, prev=$500  → price_change=-50, has search URL
    Flight 2: price=$820, no history → price_change=None
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
    """Same payload but without the XSRF safety prefix."""
    inner = [[FLIGHT_ID_1, 300]]
    frame = json.dumps(inner)
    outer = [["wrb.fr", "GetSolutionPrices", frame, None, None]]
    return json.dumps(outer).encode()


@pytest.fixture
def sample_captured_request():
    """Fake CDP-captured GetSolutionPrices POST request dict."""
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


@pytest.fixture
def sample_page_html():
    """
    Minimal saves-page HTML containing one AF_initDataCallback block
    with a flight entry matching the positional array format that
    extract_flight_metadata expects.

    Structure: [flight_id, details_array] where details_array positions:
      [1] search URL, [4] [[segment_row, ...], ["Airline"]]
    Segment row positions:
      [1] ["Airline"], [2] [["UA","100"]], [3] origin, [4] dep_date,
      [5] dep_time, [6] dest, [7] arr_date, [8] arr_time, [9] duration,
      [10] padding, [11] stopovers
    """
    segment = [
        None,                          # [0]
        ["United Airlines"],           # [1] airline
        [["UA", "100"]],               # [2] flight numbers
        "ORD",                         # [3] origin
        [2026, 4, 1],                  # [4] departure date
        [8, 30],                       # [5] departure time
        "LAX",                         # [6] destination
        [2026, 4, 1],                  # [7] arrival date
        [11, 45],                      # [8] arrival time
        195,                           # [9] duration min
        None,                          # [10]
        None,                          # [11] stopovers (null = direct)
    ]
    details = [
        None,                                          # [0]
        "/travel/flights?tfs=abc123",                  # [1] search URL
        None,                                          # [2]
        None,                                          # [3]
        [[segment], ["United Airlines"]],              # [4] segments
    ]
    # Wrap in the structure _find_flight_entries expects
    flight_entry = [FLIGHT_ID_1, details]
    data_array = [None, [flight_entry]]

    block = json.dumps({"key": "ds:1", "data": data_array})
    return f'<html><body><script>AF_initDataCallback({block});</script></body></html>'


@pytest.fixture
def mock_s3_empty():
    """S3 client that raises NoSuchKey for every get_object call."""
    from botocore.exceptions import ClientError
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "GetObject",
    )
    return s3


@pytest.fixture
def mock_s3_with_session():
    """S3 client that returns a fresh valid session payload."""
    from datetime import timezone
    s3 = MagicMock()
    payload = json.dumps({
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "cookies": [
            {"name": "SID", "value": "fake-sid", "domain": ".google.com", "path": "/"},
        ],
    })

    resp_mock = MagicMock()
    resp_mock.__getitem__ = lambda self, key: {
        "Body": MagicMock(read=lambda: payload.encode())
    }[key]
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: payload.encode())}
    return s3


@pytest.fixture
def mock_dynamodb_table():
    """DynamoDB Table mock that reports no existing price history."""
    table = MagicMock()
    table.query.return_value = {"Items": []}
    return table


# datetime needed in mock_s3_with_session above
from datetime import datetime
