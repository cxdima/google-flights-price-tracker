"""Unit tests for gfpt.config and gfpt.models — no I/O."""
import gfpt.config
from gfpt.config import load_settings, parse_telegram_users
from gfpt.models import PriceQuote, RunSummary, TelegramUser

# ── parse_telegram_users ───────────────────────────────────────────────────────

class TestParseTelegramUsers:
    def test_parses_preferred_id_name_format(self):
        users = parse_telegram_users("12345:Dmitry,67890:Alex", "")

        assert users == (
            TelegramUser(chat_id="12345", name="Dmitry"),
            TelegramUser(chat_id="67890", name="Alex"),
        )

    def test_falls_back_to_legacy_chat_ids_when_users_blank(self):
        users = parse_telegram_users("", "12345,67890")

        assert [u.chat_id for u in users] == ["12345", "67890"]

    def test_whitespace_only_users_raw_uses_legacy_fallback(self):
        users = parse_telegram_users("   ", "12345")

        assert [u.chat_id for u in users] == ["12345"]

    def test_legacy_entries_get_auto_generated_names(self):
        users = parse_telegram_users("", "123456789")

        assert users[0].name == "User 1234"

    def test_entry_without_name_gets_auto_generated_name(self):
        users = parse_telegram_users("987654321:", "")

        assert users[0].name == "User 9876"

    def test_malformed_entries_are_skipped(self):
        users = parse_telegram_users("12345:Dmitry,not-a-number:Bob,:NoId", "")

        assert [u.chat_id for u in users] == ["12345"]

    def test_duplicate_chat_ids_are_deduplicated(self):
        users = parse_telegram_users("12345:Dmitry,12345:Duplicate", "")

        assert len(users) == 1
        assert users[0].name == "Dmitry"

    def test_negative_group_chat_ids_are_valid(self):
        users = parse_telegram_users("-100123:Family Group", "")

        assert users == (TelegramUser(chat_id="-100123", name="Family Group"),)

    def test_empty_inputs_return_empty_tuple(self):
        assert parse_telegram_users("", "") == ()

    def test_blank_entries_between_commas_are_skipped(self):
        users = parse_telegram_users("12345:Dmitry,, ,67890:Alex", "")

        assert len(users) == 2

    def test_surrounding_whitespace_is_stripped(self):
        users = parse_telegram_users("  12345 : Dmitry ", "")

        assert users == (TelegramUser(chat_id="12345", name="Dmitry"),)


# ── load_settings ──────────────────────────────────────────────────────────────

class TestLoadSettings:
    def test_reads_environment_defaults_from_conftest(self):
        settings = load_settings()

        assert settings.google_email == "test@example.com"
        assert settings.s3_bucket == "test-bucket"
        assert settings.dynamodb_table == "test-prices"
        assert settings.aws_region == "us-east-1"

    def test_parses_telegram_users_from_env(self):
        settings = load_settings()

        assert [u.name for u in settings.telegram_users] == ["Dmitry", "Alex"]
        assert [u.chat_id for u in settings.telegram_users] == ["12345", "67890"]

    def test_result_is_cached_between_calls(self):
        assert load_settings() is load_settings()

    def test_cache_clear_picks_up_changed_env(self, monkeypatch):
        monkeypatch.setenv("S3_BUCKET", "other-bucket")

        gfpt.config.load_settings.cache_clear()

        assert load_settings().s3_bucket == "other-bucket"

    def test_missing_optional_vars_use_defaults(self, monkeypatch):
        monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        gfpt.config.load_settings.cache_clear()

        settings = load_settings()

        assert settings.dynamodb_table == "gfpricetracker-prices"
        assert settings.aws_region == "us-east-1"


# ── Models ─────────────────────────────────────────────────────────────────────

class TestPriceQuote:
    def test_search_url_defaults_to_none(self):
        quote = PriceQuote(flight_id="id_x", price=450)

        assert quote.search_url is None


class TestRunSummary:
    def test_to_dict_round_trips_through_from_dict(self):
        summary = RunSummary(
            ok=True, flights=3, updated=1, removed=2,
            runtime_secs=42.35, finished_at="07/08/2026 10:00", error="",
        )

        restored = RunSummary.from_dict(summary.to_dict())

        assert restored.ok is True
        assert restored.flights == 3
        assert restored.updated == 1
        assert restored.removed == 2
        assert restored.runtime_secs == 42.4  # rounded by to_dict
        assert restored.finished_at == "07/08/2026 10:00"

    def test_from_dict_maps_legacy_ts_key_to_finished_at(self):
        restored = RunSummary.from_dict({"ok": True, "ts": "03/15/2026 10:00"})

        assert restored.finished_at == "03/15/2026 10:00"

    def test_from_dict_prefers_finished_at_over_legacy_ts(self):
        restored = RunSummary.from_dict(
            {"ok": True, "finished_at": "new", "ts": "old"}
        )

        assert restored.finished_at == "new"

    def test_from_dict_returns_none_for_non_dict(self):
        assert RunSummary.from_dict(None) is None
        assert RunSummary.from_dict("not a dict") is None
        assert RunSummary.from_dict([1, 2]) is None

    def test_from_dict_defaults_missing_fields(self):
        restored = RunSummary.from_dict({})

        assert restored.ok is False
        assert restored.flights == 0
        assert restored.error == ""
