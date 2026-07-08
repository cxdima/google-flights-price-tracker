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
from gfpt.tracking.runner import TrackerDeps, run_and_report


@pytest.fixture
def deps():
    state = MagicMock()
    state.load_manifest.return_value = {}
    state.load_failure_streak.return_value = 0
    history = MagicMock()
    history.last_price.return_value = None
    return TrackerDeps(
        settings=MagicMock(),
        state=state,
        history=history,
        notifier=MagicMock(),
    )


def _wire_success(monkeypatch, quotes=None, meta=None):
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
    monkeypatch.setattr(runner_mod, "_fetch_quotes", lambda captured, headers: quotes)


def _wire_browser_failure(monkeypatch, message="Chrome exploded"):
    def _boom(settings, state):
        raise RuntimeError(message)

    monkeypatch.setattr(runner_mod, "_browser_phase", _boom)


# ── Success path ───────────────────────────────────────────────────────────────

class TestSuccessPath:
    def test_manifest_saved_with_fresh_data(self, deps, monkeypatch):
        _wire_success(monkeypatch)

        run_and_report(deps)

        deps.state.save_manifest.assert_called_once()
        manifest = deps.state.save_manifest.call_args[0][0]
        assert manifest[FLIGHT_ID_1]["price"] == 450

    def test_summary_saved_with_ok_true(self, deps, monkeypatch):
        _wire_success(monkeypatch)

        result = run_and_report(deps)

        deps.state.save_summary.assert_called_once()
        summary = deps.state.save_summary.call_args[0][0]
        assert summary["ok"] is True
        assert summary["flights"] == 1
        assert result == summary

    def test_streak_untouched_when_already_zero(self, deps, monkeypatch):
        _wire_success(monkeypatch)
        deps.state.load_failure_streak.return_value = 0

        run_and_report(deps)

        deps.state.save_failure_streak.assert_not_called()
        deps.notifier.recovery.assert_not_called()

    def test_streak_reset_when_previously_nonzero(self, deps, monkeypatch):
        _wire_success(monkeypatch)
        deps.state.load_failure_streak.return_value = 1

        run_and_report(deps)

        deps.state.save_failure_streak.assert_called_once_with(0)

    def test_recovery_not_sent_when_streak_was_below_threshold(
        self, deps, monkeypatch
    ):
        _wire_success(monkeypatch)
        deps.state.load_failure_streak.return_value = 1

        run_and_report(deps)

        deps.notifier.recovery.assert_not_called()

    def test_recovery_sent_when_streak_was_at_threshold(self, deps, monkeypatch):
        _wire_success(monkeypatch)
        deps.state.load_failure_streak.return_value = FAILURE_NOTIFY_THRESHOLD

        run_and_report(deps)

        deps.notifier.recovery.assert_called_once_with(FAILURE_NOTIFY_THRESHOLD)

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
        deps.state.load_failure_streak.return_value = 0

        run_and_report(deps)

        deps.state.save_failure_streak.assert_called_once_with(1)

    def test_no_failure_notice_on_first_failure(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch)
        deps.state.load_failure_streak.return_value = 0

        run_and_report(deps)

        deps.notifier.failure.assert_not_called()

    def test_no_failure_notice_on_second_failure(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch)
        deps.state.load_failure_streak.return_value = 1

        run_and_report(deps)

        deps.notifier.failure.assert_not_called()

    def test_failure_notice_sent_exactly_when_streak_hits_threshold(
        self, deps, monkeypatch
    ):
        _wire_browser_failure(monkeypatch, "still broken")
        deps.state.load_failure_streak.return_value = FAILURE_NOTIFY_THRESHOLD - 1

        run_and_report(deps)

        deps.state.save_failure_streak.assert_called_once_with(FAILURE_NOTIFY_THRESHOLD)
        deps.notifier.failure.assert_called_once()
        error_arg, streak_arg = deps.notifier.failure.call_args[0]
        assert "still broken" in error_arg
        assert streak_arg == FAILURE_NOTIFY_THRESHOLD

    def test_no_repeat_failure_notice_past_threshold(self, deps, monkeypatch):
        _wire_browser_failure(monkeypatch)
        deps.state.load_failure_streak.return_value = FAILURE_NOTIFY_THRESHOLD

        run_and_report(deps)

        deps.state.save_failure_streak.assert_called_once_with(
            FAILURE_NOTIFY_THRESHOLD + 1
        )
        deps.notifier.failure.assert_not_called()

    def test_zero_quotes_is_treated_as_failure(self, deps, monkeypatch):
        _wire_success(monkeypatch, quotes=[])

        result = run_and_report(deps)

        assert result["ok"] is False
        assert "No price quotes" in result["error"]
        deps.state.save_failure_streak.assert_called_once_with(1)


# ── New-low detection ──────────────────────────────────────────────────────────

class TestNewLowDetection:
    def test_price_drop_records_and_alerts_with_previous_price(
        self, deps, monkeypatch
    ):
        quote = PriceQuote(FLIGHT_ID_1, 450, SEARCH_URL_1)
        _wire_success(monkeypatch, quotes=[quote])
        deps.history.last_price.return_value = 500

        result = run_and_report(deps)

        deps.history.record.assert_called_once()
        assert deps.history.record.call_args.kwargs["prev_price"] == 500
        deps.notifier.price_alert.assert_called_once()
        alert_kwargs = deps.notifier.price_alert.call_args.kwargs
        assert alert_kwargs["last_known_price"] == 500
        assert alert_kwargs["is_new_flight"] is False
        assert result["updated"] == 1

    def test_price_rise_neither_records_nor_alerts(self, deps, monkeypatch):
        _wire_success(monkeypatch, quotes=[PriceQuote(FLIGHT_ID_1, 550)])
        deps.history.last_price.return_value = 500

        result = run_and_report(deps)

        deps.history.record.assert_not_called()
        deps.notifier.price_alert.assert_not_called()
        assert result["updated"] == 0

    def test_unchanged_price_neither_records_nor_alerts(self, deps, monkeypatch):
        _wire_success(monkeypatch, quotes=[PriceQuote(FLIGHT_ID_1, 500)])
        deps.history.last_price.return_value = 500

        run_and_report(deps)

        deps.history.record.assert_not_called()
        deps.notifier.price_alert.assert_not_called()

    def test_unseen_flight_alerts_as_new_flight(self, deps, monkeypatch):
        _wire_success(monkeypatch)
        deps.history.last_price.return_value = None

        run_and_report(deps)

        assert deps.history.record.call_args.kwargs["prev_price"] is None
        alert_kwargs = deps.notifier.price_alert.call_args.kwargs
        assert alert_kwargs["is_new_flight"] is True
        assert alert_kwargs["last_known_price"] is None
