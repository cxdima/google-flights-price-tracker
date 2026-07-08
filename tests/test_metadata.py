"""Unit tests for gfpt.tracking.metadata — AF_initDataCallback HTML parsing."""
import json

from conftest import FLIGHT_ID_1, FLIGHT_ID_2, _page_html_for, _segment_row
from gfpt.tracking.metadata import extract_flight_metadata


class TestExtractFlightMetadata:
    def test_returns_dict_keyed_by_flight_id(self, sample_page_html):
        result = extract_flight_metadata(sample_page_html)

        assert FLIGHT_ID_1 in result

    def test_extracts_route_fields(self, sample_page_html):
        meta = extract_flight_metadata(sample_page_html)[FLIGHT_ID_1]

        assert meta["origin"] == "ORD"
        assert meta["destination"] == "LAX"
        assert meta["airline"] == "United Airlines"
        assert meta["departure_time"] == "08:30"

    def test_extracts_dates_times_and_duration(self, sample_page_html):
        meta = extract_flight_metadata(sample_page_html)[FLIGHT_ID_1]

        assert meta["departure_date"] == "2026-04-01"
        assert meta["arrival_date"] == "2026-04-01"
        assert meta["arrival_time"] == "11:45"
        assert meta["duration_min"] == 195

    def test_extracts_flight_numbers(self, sample_page_html):
        meta = extract_flight_metadata(sample_page_html)[FLIGHT_ID_1]

        assert meta["flight_numbers"] == ["UA 100"]

    def test_direct_flight_has_zero_stops(self, sample_page_html):
        meta = extract_flight_metadata(sample_page_html)[FLIGHT_ID_1]

        assert meta["stops"] == 0

    def test_extracts_absolute_search_url(self, sample_page_html):
        meta = extract_flight_metadata(sample_page_html)[FLIGHT_ID_1]

        assert meta["search_url"].startswith("https://www.google.com/travel/flights")

    def test_single_slice_flight_has_no_slices_key(self, sample_page_html):
        meta = extract_flight_metadata(sample_page_html)[FLIGHT_ID_1]

        assert "slices" not in meta

    def test_empty_html_returns_empty_dict(self):
        assert extract_flight_metadata("") == {}

    def test_html_without_callbacks_returns_empty_dict(self):
        assert extract_flight_metadata("<html><body>no scripts</body></html>") == {}

    def test_block_whose_data_is_not_a_list_is_skipped(self):
        block = json.dumps({"key": "ds:9", "data": {"not": "a list"}})
        html = f"<html><script>AF_initDataCallback({block});</script></html>"

        assert extract_flight_metadata(html) == {}

    def test_bad_block_does_not_break_good_blocks(self, sample_page_html):
        bad_block = json.dumps({"key": "ds:9", "data": {"not": "a list"}})
        html = (
            f"<html><script>AF_initDataCallback({bad_block});</script>"
            + sample_page_html
        )

        result = extract_flight_metadata(html)

        assert FLIGHT_ID_1 in result

    def test_stopover_list_populates_stops_and_via(self):
        segment = _segment_row(
            "United Airlines", ["UA", "100"],
            "ORD", [2026, 4, 1], [8, 30],
            "LAX", [2026, 4, 1], [13, 45], 315,
            via=["DEN"],
        )
        html = _page_html_for(FLIGHT_ID_2, [segment])

        meta = extract_flight_metadata(html)[FLIGHT_ID_2]

        assert meta["stops"] == 1
        assert meta["via"] == ["DEN"]


class TestRoundTripMetadata:
    def test_round_trip_produces_two_slices(self, round_trip_page_html):
        meta = extract_flight_metadata(round_trip_page_html)[FLIGHT_ID_1]

        assert len(meta["slices"]) == 2

    def test_top_level_fields_mirror_first_slice(self, round_trip_page_html):
        meta = extract_flight_metadata(round_trip_page_html)[FLIGHT_ID_1]
        first = meta["slices"][0]

        assert meta["origin"] == first["origin"] == "ORD"
        assert meta["destination"] == first["destination"] == "LAX"
        assert meta["departure_date"] == first["departure_date"] == "2026-04-01"
        assert meta["flight_numbers"] == first["flight_numbers"] == ["UA 100"]

    def test_second_slice_has_own_origin_and_destination(self, round_trip_page_html):
        meta = extract_flight_metadata(round_trip_page_html)[FLIGHT_ID_1]
        second = meta["slices"][1]

        assert second["origin"] == "LAX"
        assert second["destination"] == "ORD"
        assert second["departure_date"] == "2026-04-08"
        assert second["flight_numbers"] == ["UA 205"]
