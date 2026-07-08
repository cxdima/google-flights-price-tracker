"""Unit tests for gfpt.bot.format — pure message builders."""
from conftest import FLIGHT_ID_1, FLIGHT_ID_2, SEARCH_URL_1
from gfpt.bot.format import (
    _h,
    format_flights,
    format_help,
    format_price_alert,
    format_removed_flights,
    format_status,
)
from gfpt.models import PriceQuote, RunSummary, TelegramUser

USER = TelegramUser(chat_id="12345", name="Dmitry")


def _quote(price=500, url=SEARCH_URL_1):
    return PriceQuote(flight_id=FLIGHT_ID_1, price=price, search_url=url)


def _meta(origin="ORD", dest="LAX", airline="United Airlines",
          flight_numbers=None, dep_t="08:30", arr_t="11:45",
          dep_d="2026-04-01"):
    return {
        "origin": origin,
        "destination": dest,
        "airline": airline,
        "flight_numbers": flight_numbers or ["UA 100"],
        "departure_time": dep_t,
        "arrival_time": arr_t,
        "departure_date": dep_d,
    }


# ── _h (HTML escape) ───────────────────────────────────────────────────────────

class TestHtmlEscape:
    def test_escapes_ampersand(self):
        assert _h("A & B") == "A &amp; B"

    def test_escapes_angle_brackets(self):
        assert _h("<script>") == "&lt;script&gt;"

    def test_plain_text_unchanged(self):
        assert _h("hello world") == "hello world"

    def test_dollar_sign_unchanged(self):
        assert _h("$500") == "$500"

    def test_non_string_input_is_stringified(self):
        assert _h(42) == "42"

    def test_escapes_double_quote(self):
        # A stray quote in a scraped URL would break href="..." and make
        # Telegram reject the entire message
        assert _h('x="y"') == "x=&quot;y&quot;"


# ── format_price_alert ─────────────────────────────────────────────────────────

class TestFormatPriceAlert:
    def test_new_flight_says_now_tracking(self):
        msg = format_price_alert(_quote(), {}, is_new_flight=True)

        assert "Now tracking" in msg

    def test_price_drop_says_new_low_with_previous_price(self):
        msg = format_price_alert(_quote(price=400), {}, last_known_price=500)

        assert "New low!" in msg
        assert "was $500" in msg

    def test_price_uses_thousands_separator(self):
        msg = format_price_alert(_quote(price=1234), {}, is_new_flight=True)

        assert "1,234" in msg

    def test_route_line_present_with_metadata(self):
        msg = format_price_alert(_quote(), _meta(), is_new_flight=True)

        assert "ORD → LAX" in msg

    def test_route_line_absent_without_metadata(self):
        msg = format_price_alert(_quote(), {}, is_new_flight=True)

        assert "ORD" not in msg
        assert "→" not in msg.replace("08:30 → 11:45", "")

    def test_airline_appears(self):
        msg = format_price_alert(_quote(), _meta(), is_new_flight=True)

        assert "United Airlines" in msg

    def test_round_trip_renders_bidirectional_arrow(self):
        meta = {
            **_meta(),
            "slices": [
                {"origin": "ORD", "destination": "LAX"},
                {"origin": "LAX", "destination": "ORD"},
            ],
        }

        msg = format_price_alert(_quote(), meta, is_new_flight=True)

        assert "ORD ⇄ LAX" in msg

    def test_one_way_multi_city_keeps_single_arrow(self):
        meta = {
            **_meta(),
            "slices": [
                {"origin": "ORD", "destination": "LAX"},
                {"origin": "LAX", "destination": "SFO"},  # not back to ORD
            ],
        }

        msg = format_price_alert(_quote(), meta, is_new_flight=True)

        assert "ORD → LAX" in msg
        assert "⇄" not in msg

    def test_search_url_becomes_link(self):
        msg = format_price_alert(_quote(url=SEARCH_URL_1), {}, is_new_flight=True)

        assert f'href="{SEARCH_URL_1}"' in msg

    def test_metadata_is_html_escaped(self):
        meta = _meta(airline="Fly & Co <cheap>")

        msg = format_price_alert(_quote(), meta, is_new_flight=True)

        assert "Fly &amp; Co &lt;cheap&gt;" in msg
        assert "<cheap>" not in msg


# ── format_status ──────────────────────────────────────────────────────────────

class TestFormatStatus:
    def _summary(self, **overrides):
        base = dict(
            ok=True, flights=3, updated=2, removed=1,
            runtime_secs=42.0, finished_at="07/08/2026 09:00", error="",
        )
        return RunSummary(**{**base, **overrides})

    def test_no_run_data_mentions_user_by_name(self):
        msg = format_status(None, USER, muted=False)

        assert "No run data" in msg
        assert "Dmitry" in msg

    def test_shows_flight_count(self):
        msg = format_status(self._summary(), USER, muted=False)

        assert "3</b> flights tracked" in msg

    def test_shows_updated_count(self):
        msg = format_status(self._summary(updated=2), USER, muted=False)

        assert "2</b> new lows" in msg

    def test_shows_removed_count_when_nonzero(self):
        msg = format_status(self._summary(removed=1), USER, muted=False)

        assert "1 removed" in msg

    def test_hides_removed_line_when_zero(self):
        msg = format_status(self._summary(removed=0), USER, muted=False)

        assert "removed" not in msg

    def test_muted_user_sees_paused_hint(self):
        msg = format_status(self._summary(), USER, muted=True)

        assert "paused" in msg
        assert "/resume" in msg

    def test_unmuted_user_sees_no_paused_hint(self):
        msg = format_status(self._summary(), USER, muted=False)

        assert "paused" not in msg

    def test_failed_run_shows_error(self):
        msg = format_status(
            self._summary(ok=False, error="Chrome crashed"), USER, muted=False,
        )

        assert "Chrome crashed" in msg


# ── format_flights ─────────────────────────────────────────────────────────────

class TestFormatFlights:
    def test_empty_manifest_shows_friendly_message(self):
        assert "No flights tracked yet" in format_flights({})

    def test_dunder_keys_are_skipped(self):
        msg = format_flights({"__updated__": "07/08/2026 09:00"})

        assert "No flights tracked yet" in msg

    def test_flight_shows_route_and_price(self):
        manifest = {FLIGHT_ID_1: {**_meta(), "price": 450}}

        msg = format_flights(manifest)

        assert "ORD → LAX" in msg
        assert "$450" in msg

    def test_flights_on_same_route_and_date_share_one_header(self):
        manifest = {
            FLIGHT_ID_1: {**_meta(), "price": 450},
            FLIGHT_ID_2: {**_meta(flight_numbers=["UA 200"], dep_t="14:00"),
                          "price": 510},
        }

        msg = format_flights(manifest)

        assert msg.count("🛫") == 1
        assert "$450" in msg
        assert "$510" in msg

    def test_different_dates_get_separate_headers(self):
        manifest = {
            FLIGHT_ID_1: {**_meta(), "price": 450},
            FLIGHT_ID_2: {**_meta(dep_d="2026-05-01"), "price": 510},
        }

        msg = format_flights(manifest)

        assert msg.count("🛫") == 2

    def test_meta_less_entry_lands_in_details_pending_section(self):
        manifest = {FLIGHT_ID_1: {"price": 450}}

        msg = format_flights(manifest)

        assert "Details pending" in msg
        assert FLIGHT_ID_1[:14] in msg
        assert "$450" in msg
        assert "???" not in msg

    def test_meta_less_entry_without_price_says_price_unknown(self):
        msg = format_flights({FLIGHT_ID_1: {}})

        assert "price unknown" in msg

    def test_last_updated_stamp_included(self):
        manifest = {FLIGHT_ID_1: {**_meta(), "price": 450},
                    "__updated__": "07/08/2026 09:00"}

        msg = format_flights(manifest)

        assert "Last updated: 07/08/2026 09:00" in msg


# ── format_removed_flights ─────────────────────────────────────────────────────

class TestFormatRemovedFlights:
    def test_renders_each_removed_flight(self):
        removed = {
            FLIGHT_ID_1: _meta(),
            FLIGHT_ID_2: _meta(origin="SFO", dest="JFK",
                               flight_numbers=["DL 42"]),
        }

        msg = format_removed_flights(removed)

        assert "No longer tracking" in msg
        assert "(2)" in msg
        assert "ORD → LAX" in msg
        assert "SFO → JFK" in msg

    def test_entry_without_route_says_unknown_route(self):
        msg = format_removed_flights({FLIGHT_ID_1: {}})

        assert "Unknown route" in msg


# ── format_help ────────────────────────────────────────────────────────────────

class TestFormatHelp:
    def test_lists_all_commands(self):
        msg = format_help(USER)

        for command in ("/status", "/flights", "/pause", "/resume"):
            assert command in msg

    def test_greets_user_by_name(self):
        assert "Dmitry" in format_help(USER)
