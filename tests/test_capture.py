"""Unit tests for gfpt.tracking.capture — CDP body collection and session
headers, fake driver, no Chrome."""
import base64
from unittest.mock import MagicMock

import gfpt.tracking.capture as capture_mod
from gfpt.config import CHROME_USER_AGENT
from gfpt.tracking.capture import _collect_response_bodies, build_session_headers


class TestCollectResponseBodies:
    def test_returns_decoded_bodies(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.return_value = {
            "body": "plain text", "base64Encoded": False,
        }

        bodies = _collect_response_bodies(driver, ["req-1"])

        assert bodies == [b"plain text"]

    def test_decodes_base64_bodies(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.return_value = {
            "body": base64.b64encode(b"\x00binary").decode(),
            "base64Encoded": True,
        }

        bodies = _collect_response_bodies(driver, ["req-1"])

        assert bodies == [b"\x00binary"]

    def test_retries_while_response_still_streaming(self, monkeypatch):
        monkeypatch.setattr(capture_mod.time, "sleep", lambda s: None)
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = [
            Exception("No data found for resource"),  # still streaming
            {"body": "late body", "base64Encoded": False},
        ]

        bodies = _collect_response_bodies(driver, ["req-1"])

        assert bodies == [b"late body"]

    def test_gives_up_after_attempts_and_keeps_alignment(self, monkeypatch):
        """A missing body must stay visible as None at its own index —
        the runner re-fires exactly those requests."""
        monkeypatch.setattr(capture_mod.time, "sleep", lambda s: None)
        driver = MagicMock()

        def cdp(cmd, args):
            if args["requestId"] == "req-bad":
                raise Exception("evicted")
            return {"body": "good", "base64Encoded": False}

        driver.execute_cdp_cmd.side_effect = cdp

        bodies = _collect_response_bodies(driver, ["req-good", "req-bad"])

        assert bodies == [b"good", None]

    def test_decode_failure_is_treated_as_missing(self, monkeypatch):
        monkeypatch.setattr(capture_mod.time, "sleep", lambda s: None)
        driver = MagicMock()
        driver.execute_cdp_cmd.return_value = {
            "body": "!!! not base64 !!!", "base64Encoded": True,
        }

        bodies = _collect_response_bodies(driver, ["req-1"])

        assert bodies == [None]

    def test_blank_request_ids_stay_none(self):
        driver = MagicMock()

        assert _collect_response_bodies(driver, ["", ""]) == [None, None]
        driver.execute_cdp_cmd.assert_not_called()


class TestBuildSessionHeaders:
    def _driver(self):
        driver = MagicMock()
        driver.get_cookies.return_value = [
            {"name": "SID", "value": "abc"},
            {"name": "HSID", "value": "def"},
        ]
        driver.execute_script.return_value = CHROME_USER_AGENT
        return driver

    def test_chrome_fidelity_headers_present(self):
        headers = build_session_headers(self._driver())

        assert headers["Cookie"] == "SID=abc; HSID=def"
        assert headers["Sec-Fetch-Site"] == "same-origin"
        assert headers["Sec-Fetch-Mode"] == "cors"
        assert headers["Accept-Language"].startswith("en-US")

    def test_sec_ch_ua_matches_configured_chrome_major(self):
        headers = build_session_headers(self._driver())

        major = CHROME_USER_AGENT.split("Chrome/")[1].split(".")[0]
        assert f'v="{major}"' in headers["sec-ch-ua"]
