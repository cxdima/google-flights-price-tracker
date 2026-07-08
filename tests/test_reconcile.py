"""
Unit tests for gfpt.tracking.reconcile — the stale-flight pruning fix.

The old tracker never removed flights from the manifest; these tests pin
down the new lifecycle: seen → missing (grace run) → removed, plus the
safety valve for empty scrapes.
"""
import copy

from conftest import FLIGHT_ID_1, FLIGHT_ID_2, SEARCH_URL_1
from gfpt.config import MISSING_RUNS_BEFORE_REMOVAL
from gfpt.models import PriceQuote
from gfpt.tracking.reconcile import reconcile_manifest

UPDATED_AT = "07/08/2026 09:00"

ORD_LAX_META = {
    "origin": "ORD", "destination": "LAX",
    "airline": "United Airlines", "departure_date": "2026-04-01",
}


def _quote(fid=FLIGHT_ID_1, price=450, url=SEARCH_URL_1):
    return PriceQuote(flight_id=fid, price=price, search_url=url)


class TestNewFlights:
    def test_new_flight_from_meta_and_quote_enters_manifest(self):
        result = reconcile_manifest(
            {}, {FLIGHT_ID_1: dict(ORD_LAX_META)}, [_quote(price=450)], UPDATED_AT,
        )

        entry = result.manifest[FLIGHT_ID_1]
        assert entry["price"] == 450
        assert entry["missing_runs"] == 0
        assert entry["origin"] == "ORD"
        assert entry["search_url"] == SEARCH_URL_1
        assert result.removed == {}

    def test_flight_seen_only_in_quotes_still_enters_manifest(self):
        result = reconcile_manifest({}, {}, [_quote(price=300)], UPDATED_AT)

        assert result.manifest[FLIGHT_ID_1]["price"] == 300
        assert result.manifest[FLIGHT_ID_1]["missing_runs"] == 0


class TestMissingRunLifecycle:
    def test_removal_threshold_constant_is_two(self):
        assert MISSING_RUNS_BEFORE_REMOVAL == 2

    def test_flight_absent_one_run_kept_with_missing_runs_1(self):
        old = {FLIGHT_ID_1: {**ORD_LAX_META, "missing_runs": 0}}

        result = reconcile_manifest(
            old, {FLIGHT_ID_2: {"origin": "SFO"}}, [_quote(fid=FLIGHT_ID_2)], UPDATED_AT,
        )

        assert result.manifest[FLIGHT_ID_1]["missing_runs"] == 1
        assert FLIGHT_ID_1 not in result.removed

    def test_removed_flight_pruned_after_two_missing_runs(self):
        old = {FLIGHT_ID_1: {**ORD_LAX_META, "missing_runs": 1}}

        result = reconcile_manifest(
            old, {FLIGHT_ID_2: {"origin": "SFO"}}, [_quote(fid=FLIGHT_ID_2)], UPDATED_AT,
        )

        assert FLIGHT_ID_1 not in result.manifest
        assert FLIGHT_ID_1 in result.removed
        assert result.removed[FLIGHT_ID_1]["origin"] == "ORD"

    def test_reappearing_flight_resets_missing_runs(self):
        old = {FLIGHT_ID_1: {**ORD_LAX_META, "missing_runs": 1}}

        result = reconcile_manifest(
            old, {FLIGHT_ID_1: dict(ORD_LAX_META)}, [_quote()], UPDATED_AT,
        )

        assert result.manifest[FLIGHT_ID_1]["missing_runs"] == 0
        assert result.removed == {}

    def test_reappearance_in_quotes_alone_resets_missing_runs(self):
        old = {FLIGHT_ID_1: {**ORD_LAX_META, "missing_runs": 1}}

        result = reconcile_manifest(old, {}, [_quote()], UPDATED_AT)

        assert result.manifest[FLIGHT_ID_1]["missing_runs"] == 0


class TestEmptyScrapeSafetyValve:
    def test_empty_meta_and_quotes_prunes_nothing(self):
        old = {
            FLIGHT_ID_1: {**ORD_LAX_META, "missing_runs": 1},
            FLIGHT_ID_2: {"origin": "SFO", "missing_runs": 0},
        }

        result = reconcile_manifest(old, {}, [], UPDATED_AT)

        assert result.removed == {}
        assert FLIGHT_ID_1 in result.manifest
        assert FLIGHT_ID_2 in result.manifest

    def test_empty_scrape_does_not_increment_missing_runs(self):
        old = {FLIGHT_ID_1: {**ORD_LAX_META, "missing_runs": 1}}

        result = reconcile_manifest(old, {}, [], UPDATED_AT)

        assert result.manifest[FLIGHT_ID_1]["missing_runs"] == 1


class TestPurity:
    def test_old_manifest_is_not_mutated(self):
        old = {
            FLIGHT_ID_1: {**ORD_LAX_META, "price": 500, "missing_runs": 1},
            "__updated__": "some old time",
        }
        snapshot = copy.deepcopy(old)

        reconcile_manifest(
            old, {FLIGHT_ID_1: dict(ORD_LAX_META)}, [_quote(price=450)], UPDATED_AT,
        )

        assert old == snapshot


class TestInternalKeys:
    def test_updated_key_set_to_updated_at(self):
        result = reconcile_manifest(
            {"__updated__": "stale"}, {FLIGHT_ID_1: dict(ORD_LAX_META)}, [], UPDATED_AT,
        )

        assert result.manifest["__updated__"] == UPDATED_AT

    def test_dunder_keys_are_not_treated_as_flights(self):
        old = {"__updated__": "stale", "__junk__": {"missing_runs": 5}}

        result = reconcile_manifest(
            old, {FLIGHT_ID_1: dict(ORD_LAX_META)}, [], UPDATED_AT,
        )

        assert "__junk__" not in result.removed
        assert "__junk__" not in result.manifest

    def test_non_dict_entries_are_ignored(self):
        old = {FLIGHT_ID_2: "corrupted string entry"}

        result = reconcile_manifest(
            old, {FLIGHT_ID_1: dict(ORD_LAX_META)}, [], UPDATED_AT,
        )

        assert FLIGHT_ID_2 not in result.manifest
        assert FLIGHT_ID_2 not in result.removed


class TestQuoteMerging:
    def test_quote_price_overwrites_old_price(self):
        old = {FLIGHT_ID_1: {**ORD_LAX_META, "price": 500, "missing_runs": 0}}

        result = reconcile_manifest(old, {}, [_quote(price=450)], UPDATED_AT)

        assert result.manifest[FLIGHT_ID_1]["price"] == 450

    def test_quote_without_search_url_keeps_old_search_url(self):
        old = {
            FLIGHT_ID_1: {
                **ORD_LAX_META, "price": 500,
                "search_url": SEARCH_URL_1, "missing_runs": 0,
            }
        }

        result = reconcile_manifest(old, {}, [_quote(price=450, url=None)], UPDATED_AT)

        assert result.manifest[FLIGHT_ID_1]["search_url"] == SEARCH_URL_1

    def test_quote_with_search_url_overwrites_old_one(self):
        old = {FLIGHT_ID_1: {"search_url": "https://old.example", "missing_runs": 0}}
        new_url = "https://www.google.com/travel/flights?tfs=new"

        result = reconcile_manifest(old, {}, [_quote(url=new_url)], UPDATED_AT)

        assert result.manifest[FLIGHT_ID_1]["search_url"] == new_url

    def test_fresh_meta_is_merged_over_old_entry(self):
        old = {FLIGHT_ID_1: {"origin": "OLD", "price": 500, "missing_runs": 0}}

        result = reconcile_manifest(
            old, {FLIGHT_ID_1: dict(ORD_LAX_META)}, [], UPDATED_AT,
        )

        entry = result.manifest[FLIGHT_ID_1]
        assert entry["origin"] == "ORD"
        assert entry["price"] == 500  # old price survives when no quote
