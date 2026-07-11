"""
Unit tests for gfpt.tracking.runner.run_and_report — fully mocked deps,
browser phase and quote fetching monkeypatched. No Chrome, no AWS.
"""
from unittest.mock import MagicMock

import pytest

import gfpt.tracking.runner as runner_mod
from conftest import FLIGHT_ID_1, SEARCH_URL_1
from gfpt.config import FAILURE_NOTIFY_THRESHOLD
from gfpt.models import PriceQuote
from gfpt.tracking.auth import LoginFailedError
from gfpt.tracking.runner import TrackerDeps, run_and_report


@pytest.fixture
def deps():
    state = MagicMock()
    state.load_manifest.return_value = {}
    state.load_health.return_value = {}
    state.save_manifest.return_value = True
    state.save_health.return_value = True
    state.save_summary.return_value = True
    history = MagicMock()
    history.last_price.return_value = None
    notifier = MagicMock()
    notifier.failure.return_value = True
    return TrackerDeps(
        settings=MagicMock(),
        state=state,
        history=history,
        notifier=notifier,
    )


def _wire_success(monkeypatch, quotes=None, meta=None, refire_failures=0):
    """Make the browser phase and quote fetching succeed deterministically."""
    if quotes is None:
        quotes = [PriceQuote(FLIGHT_ID_1, 450, SEARCH_URL_1)]
    if meta is None:
        meta = {FLIGHT_ID_1: {"origin": "ORD", "destination": "LAX"}}
    monkeypatch.setattr(
        runner_mod, "_browser_phase",
        lambda settings, state: ([{"url": "captured"}], "<html/>", {"Cookie": "x"}),
    )
    monkeypatch.setattr(runner_mod, "extract_flight_metadata", lambda html: meta)
    monkeypatch.setattr(
        runner_mod, "_fetch_quotes",
        lambda captured, headers: (quotes, refire_failures),
    )


def _wire_browser_failure(monkeypatch, message="Chrome exploded", exc_type=RuntimeError):
    def _boom(settings, state):
        raise exc_type(message)

    monkeypatch.setattr(runner_mod, "_browser_phase", _boom)


def _final_health(deps) -> dict:
    return deps.state.save_health.call_args[0][0]


# ── Success path ───────────────────────────────────────────────────────────────

class TestSuccessPath:
    def test_manifest_saved_with_price_and_watermark(self, deps, monkeypatch):
        _wire_success(monkeypatch)

        run_and_report(deps)

        deps.state.save_manifest.assert_called_once()
        manifest = deps.state.save_manifest.call_args[0][0]
        assert manifest[FLIGHT_ID_1]["price"] == 450
        assert manifest[FLIGHT_ID_1]["low_price"] == 450

    def test_summary_saved_with_ok_true(self, deps, monkeypatch):
        _wire_success(monkeypatch)

        result = run_and_report(deps)

        deps.state.save_summary.assert_called_once()
        summary = deps.state.save_summary.call_args[0][0]
        assert summary["ok"] is True
        assert summary["flights"] == 1
        assert result == summary

    def test_streak_written_ahead_then_reset(self, deps, monkeypatch):
        """The streak is incremented BEFORE the run (so a timeout-killed
        process still counts) and reset to zero on success."""
        _wire_success(monkeypatch)
        deps.state.load_health.return_value = {"consecutive_failures": 1}

        run_and_report(deps)

        first = deps.state.save_health.call_args_list[0][0][0]
        assert first["consecutive_failures"] == 2
        assert _final_health(deps)["consecutive_failures"] == 0

    def test_success_resets_login_failures_and_notified_flag(self, deps, monkeypatch):
        _wire_success(monkeypatch)
        deps.state.load_health.return_value = {
            "consecutive_failures": 5, "failure_notified": True, "login_failures": 4,
        }

        run_and_report(deps)

        final = _final_health(deps)
        assert final["consecutive_failures"] == 0
        assert final["failure_notified"] is False
        assert final["login_failures"] == 0

    def test_recovery_sent_only_when_failure_was_notified(self, deps, monkeypatch):
        _wire_success(monkeypatch)
        deps.state.load_health.return_value = {
            "consecutive_failures": 5, "failure_notified": True,
        }

        run_and_report(deps)

        deps.notifier.recovery.assert_called_once_with(5)

    def test_no_recovery_when_failures_were_never_notified(self, deps, monkeypatch):
        _wire_success(monkeypatch)
        deps.state.load_health.return_value = {"consecutive_failures": 2}

        run_and_report(deps)

        deps.notifier.recovery.assert_not_called()

    def test_removed_flights_trigger_removal_notice(self, deps, monkeypatch):
        _wire_success(monkeypatch)
        stale_id = "id_" + "f" * 32
        deps.state.load_manifest.return_value = {
            stale_id: {"origin": "SFO", "missing_runs": 1},
        }

        result = run_and_report(deps)

        deps.notifier.flights_removed.assert_called_once()
        assert stale_id in deps.notifier.flights_removed.call_args[0][0]
        assert result["removed"] == 1

    def test_manifest_write_failure_fails_the_run(self, deps, monkeypatch):
        """S3 breakage must land in the failure streak, not silently degrade."""
        _wire_success(monkeypatch)
        deps.state.save_manifest.return_value = False

        result = run_and_report(deps)

        assert result["ok"] is False
        assert "manifest write failed" in result["error"]
        deps.notifier.price_alert.assert_not_called()

    def test_summary_line_logged_on_success(self, deps, monkeypatch, caplog):
        _wire_success(monkeypatch)

        with caplog.at_level("INFO"):
            run_and_report(deps)

        assert any("GFPT_SUMMARY" in r.message and '"ok": true' in r.message
                   for r in caplog.records)


# ── Failure path ───────────────────────────────────────────────────────────────

class TestFailurePath:
    def test_summary_saved_with_error_text(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch, "Chrome exploded")

        result = run_and_report(deps)

        summary = deps.state.save_summary.call_args[0][0]
        assert summary["ok"] is False
        assert "Chrome exploded" in summary["error"]
        assert result["ok"] is False

    def test_streak_incremented_on_failure(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch)

        run_and_report(deps)

        assert _final_health(deps)["consecutive_failures"] == 1

    def test_no_failure_notice_below_threshold(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch)
        deps.state.load_health.return_value = {
            "consecutive_failures": FAILURE_NOTIFY_THRESHOLD - 2,
        }

        run_and_report(deps)

        deps.notifier.failure.assert_not_called()

    def test_failure_notice_sent_when_streak_hits_threshold(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch, "still broken")
        deps.state.load_health.return_value = {
            "consecutive_failures": FAILURE_NOTIFY_THRESHOLD - 1,
        }

        run_and_report(deps)

        deps.notifier.failure.assert_called_once()
        error_arg, streak_arg = deps.notifier.failure.call_args[0]
        assert "still broken" in error_arg
        assert streak_arg == FAILURE_NOTIFY_THRESHOLD
        assert _final_health(deps)["failure_notified"] is True

    def test_notice_retried_past_threshold_if_delivery_failed(self, deps, monkeypatch):
        """A dropped Telegram send at exactly the threshold must not silence
        the notice forever — it retries while the flag is unset."""
        _wire_browser_failure(monkeypatch)
        deps.notifier.failure.return_value = False
        deps.state.load_health.return_value = {
            "consecutive_failures": FAILURE_NOTIFY_THRESHOLD + 3,
        }

        run_and_report(deps)

        deps.notifier.failure.assert_called_once()
        assert _final_health(deps).get("failure_notified") is not True

    def test_no_repeat_notice_once_delivered(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch)
        deps.state.load_health.return_value = {
            "consecutive_failures": FAILURE_NOTIFY_THRESHOLD,
            "failure_notified": True,
        }

        run_and_report(deps)

        deps.notifier.failure.assert_not_called()

    def test_zero_quotes_is_treated_as_failure(self, deps, monkeypatch):
        _wire_success(monkeypatch, quotes=[])

        result = run_and_report(deps)

        assert result["ok"] is False
        assert "No price quotes" in result["error"]
        assert _final_health(deps)["consecutive_failures"] == 1

    def test_login_failure_increments_login_counter(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch, "challenge shown",
                              exc_type=LoginFailedError)
        deps.state.load_health.return_value = {"login_failures": 1}

        run_and_report(deps)

        assert _final_health(deps)["login_failures"] == 2

    def test_generic_failure_does_not_touch_login_counter(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch)
        deps.state.load_health.return_value = {"login_failures": 1}

        run_and_report(deps)

        assert _final_health(deps).get("login_failures") == 1

    def test_summary_line_logged_on_failure(self, deps, monkeypatch, caplog):
        """The CloudWatch metric filter alarms on this exact line — it must
        be emitted even when the run fails."""
        _wire_browser_failure(monkeypatch)

        with caplog.at_level("INFO"):
            run_and_report(deps)

        assert any("GFPT_SUMMARY" in r.message and '"ok": false' in r.message
                   for r in caplog.records)


# ── Watermark / new-low detection ──────────────────────────────────────────────

def _manifest_entry(price=500, low=500, **extra):
    return {"origin": "ORD", "destination": "LAX", "price": price,
            "low_price": low, "missing_runs": 0, **extra}


class TestNewLowDetection:
    def test_drop_below_watermark_records_and_alerts(self, deps, monkeypatch):
        quote = PriceQuote(FLIGHT_ID_1, 450, SEARCH_URL_1)
        _wire_success(monkeypatch, quotes=[quote])
        deps.state.load_manifest.return_value = {FLIGHT_ID_1: _manifest_entry()}

        result = run_and_report(deps)

        deps.history.last_price.assert_not_called()  # watermark, not DDB
        deps.history.record.assert_called_once()
        assert deps.history.record.call_args.kwargs["prev_price"] == 500
        alert_kwargs = deps.notifier.price_alert.call_args.kwargs
        assert alert_kwargs["last_known_price"] == 500
        assert alert_kwargs["is_new_flight"] is False
        assert result["updated"] == 1
        manifest = deps.state.save_manifest.call_args[0][0]
        assert manifest[FLIGHT_ID_1]["low_price"] == 450

    def test_watermark_seeded_from_history_when_absent(self, deps, monkeypatch):
        quote = PriceQuote(FLIGHT_ID_1, 450, SEARCH_URL_1)
        _wire_success(monkeypatch, quotes=[quote])
        deps.state.load_manifest.return_value = {
            FLIGHT_ID_1: {"origin": "ORD", "price": 500, "missing_runs": 0},
        }
        deps.history.last_price.return_value = 500

        run_and_report(deps)

        deps.history.last_price.assert_called_once_with(FLIGHT_ID_1)
        deps.notifier.price_alert.assert_called_once()
        manifest = deps.state.save_manifest.call_args[0][0]
        assert manifest[FLIGHT_ID_1]["low_price"] == 450

    def test_history_read_failure_skips_flight_without_alert(self, deps, monkeypatch):
        """A throttled read must not masquerade as 'new flight' — that used
        to re-announce the flight and corrupt the watermark upward."""
        _wire_success(monkeypatch)
        deps.history.last_price.side_effect = LookupError("throttled")

        result = run_and_report(deps)

        assert result["ok"] is True
        deps.history.record.assert_not_called()
        deps.notifier.price_alert.assert_not_called()
        manifest = deps.state.save_manifest.call_args[0][0]
        assert "low_price" not in manifest[FLIGHT_ID_1]

    def test_rise_above_watermark_records_without_alert(self, deps, monkeypatch):
        _wire_success(monkeypatch, quotes=[PriceQuote(FLIGHT_ID_1, 550)])
        deps.state.load_manifest.return_value = {
            FLIGHT_ID_1: _manifest_entry(price=520, low=500),
        }

        result = run_and_report(deps)

        deps.history.record.assert_called_once()
        assert deps.history.record.call_args.kwargs["prev_price"] == 520
        deps.notifier.price_alert.assert_not_called()
        deps.notifier.price_rise.assert_not_called()  # wasn't sitting at the low
        assert result["updated"] == 0

    def test_rebound_off_the_low_fires_rise_event(self, deps, monkeypatch):
        _wire_success(monkeypatch, quotes=[PriceQuote(FLIGHT_ID_1, 550)])
        deps.state.load_manifest.return_value = {
            FLIGHT_ID_1: _manifest_entry(price=500, low=500),
        }

        run_and_report(deps)

        deps.notifier.price_rise.assert_called_once()
        assert deps.notifier.price_rise.call_args.kwargs["low_price"] == 500
        manifest = deps.state.save_manifest.call_args[0][0]
        assert manifest[FLIGHT_ID_1]["low_price"] == 500  # watermark untouched

    def test_unchanged_price_neither_records_nor_alerts(self, deps, monkeypatch):
        _wire_success(monkeypatch, quotes=[PriceQuote(FLIGHT_ID_1, 500)])
        deps.state.load_manifest.return_value = {FLIGHT_ID_1: _manifest_entry()}

        run_and_report(deps)

        deps.history.record.assert_not_called()
        deps.notifier.price_alert.assert_not_called()
        deps.notifier.price_rise.assert_not_called()

    def test_unseen_flight_alerts_as_new_flight(self, deps, monkeypatch):
        _wire_success(monkeypatch)
        deps.history.last_price.return_value = None

        run_and_report(deps)

        assert deps.history.record.call_args.kwargs["prev_price"] is None
        alert_kwargs = deps.notifier.price_alert.call_args.kwargs
        assert alert_kwargs["is_new_flight"] is True
        assert alert_kwargs["last_known_price"] is None


# ── Partial-scrape prune guard ─────────────────────────────────────────────────

class TestPartialRunGuard:
    def test_refire_failure_suspends_pruning(self, deps, monkeypatch):
        """A flight one miss from pruning must survive a partial run —
        two correlated partial scrapes were deleting live flights."""
        _wire_success(monkeypatch, refire_failures=1)
        stale_id = "id_" + "e" * 32
        deps.state.load_manifest.return_value = {
            stale_id: {"origin": "SFO", "missing_runs": 1},
        }

        result = run_and_report(deps)

        deps.notifier.flights_removed.assert_not_called()
        assert result["removed"] == 0
        manifest = deps.state.save_manifest.call_args[0][0]
        assert manifest[stale_id]["missing_runs"] == 1  # not advanced either

    def test_empty_metadata_with_existing_manifest_suspends_pruning(
        self, deps, monkeypatch
    ):
        _wire_success(monkeypatch, meta={})
        stale_id = "id_" + "e" * 32
        deps.state.load_manifest.return_value = {
            stale_id: {"origin": "SFO", "missing_runs": 1},
        }

        result = run_and_report(deps)

        deps.notifier.flights_removed.assert_not_called()
        assert result["removed"] == 0


class TestReboundDamping:
    def test_second_bounce_off_same_low_does_not_realert(self, deps, monkeypatch):
        """450→470 rebounds once; a later 450→470 flap off the SAME low
        stays silent for rise-subscribers."""
        _wire_success(monkeypatch, quotes=[PriceQuote(FLIGHT_ID_1, 470)])
        deps.state.load_manifest.return_value = {
            FLIGHT_ID_1: _manifest_entry(price=450, low=450, rebounded=True),
        }

        run_and_report(deps)

        deps.notifier.price_rise.assert_not_called()
        deps.history.record.assert_called_once()  # still recorded for history

    def test_new_low_rearms_the_rebound(self, deps, monkeypatch):
        _wire_success(monkeypatch, quotes=[PriceQuote(FLIGHT_ID_1, 400)])
        deps.state.load_manifest.return_value = {
            FLIGHT_ID_1: _manifest_entry(price=450, low=450, rebounded=True),
        }

        run_and_report(deps)

        manifest = deps.state.save_manifest.call_args[0][0]
        assert "rebounded" not in manifest[FLIGHT_ID_1]
        assert manifest[FLIGHT_ID_1]["low_price"] == 400

    def test_seeded_watermark_persists_even_when_price_unchanged(
        self, deps, monkeypatch
    ):
        """Live-ops regression: unchanged prices left low_price unset, so
        every run re-queried DynamoDB and /flights never showed the low."""
        _wire_success(monkeypatch, quotes=[PriceQuote(FLIGHT_ID_1, 500)])
        deps.state.load_manifest.return_value = {
            FLIGHT_ID_1: {"origin": "ORD", "price": 500, "missing_runs": 0},
        }
        deps.history.last_price.return_value = 500

        run_and_report(deps)

        manifest = deps.state.save_manifest.call_args[0][0]
        assert manifest[FLIGHT_ID_1]["low_price"] == 500
        deps.notifier.price_alert.assert_not_called()  # unchanged: no alert
