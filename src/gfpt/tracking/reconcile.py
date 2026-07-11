"""
Manifest reconciliation — the single source of truth for which flights are
being tracked.

This fixes the long-standing bug where flights removed from the Google
Flights saved list lived in the manifest (and /flights output) forever:
the old code only ever merged new data in and never pruned.

Rules:
  - A flight seen in this run's page metadata or price response is alive:
    its data is merged and its missing_runs counter resets.
  - A flight in the manifest but absent from this run gets missing_runs+1.
    After MISSING_RUNS_BEFORE_REMOVAL consecutive absences it is removed —
    one flaky scrape (missed scroll batch, partial page) can't wipe the list.
  - If the run produced no flights at all, nothing is pruned: an empty
    result is far more likely a broken scrape than an emptied saved list.

Pure function, no I/O; returns a new manifest (inputs are not mutated).
"""
from __future__ import annotations

import logging

from gfpt.config import MISSING_RUNS_BEFORE_REMOVAL
from gfpt.models import PriceQuote, ReconcileResult

log = logging.getLogger(__name__)

_INTERNAL_KEY_PREFIX = "__"


def reconcile_manifest(
    old_manifest: dict,
    flight_meta: dict[str, dict],
    quotes: list[PriceQuote],
    updated_at: str,
    prune_allowed: bool = True,
) -> ReconcileResult:
    """
    prune_allowed=False marks this run's view as PARTIAL (a re-fire batch
    failed, or the metadata parser returned nothing for a non-empty
    manifest): "absent" is then meaningless, so missing_runs counters are
    left untouched. Without this, two consecutive partial scrapes — the
    correlated-failure case — would silently prune live flights.
    """
    quotes_by_id = {q.flight_id: q for q in quotes}
    seen_ids = set(flight_meta) | set(quotes_by_id)

    new_manifest: dict = {}
    removed: dict = {}

    # Nothing seen at all → keep the old manifest untouched (minus timestamp
    # refresh); the runner treats a zero-flight run as a failure anyway.
    prune_allowed = prune_allowed and bool(seen_ids)

    old_flights = {
        fid: entry for fid, entry in old_manifest.items()
        if not fid.startswith(_INTERNAL_KEY_PREFIX) and isinstance(entry, dict)
    }

    # ── Carry forward / prune existing entries ────────────────────────────────
    for fid, entry in old_flights.items():
        if fid in seen_ids:
            new_manifest[fid] = {**entry, "missing_runs": 0}
            continue
        if not prune_allowed:
            new_manifest[fid] = dict(entry)
            continue
        misses = int(entry.get("missing_runs") or 0) + 1
        if misses >= MISSING_RUNS_BEFORE_REMOVAL:
            removed[fid] = dict(entry)
            log.info("Pruned %s after %d missing runs", fid[:16], misses)
        else:
            new_manifest[fid] = {**entry, "missing_runs": misses}
            log.info("Flight %s missing (%d/%d) — keeping for now",
                     fid[:16], misses, MISSING_RUNS_BEFORE_REMOVAL)

    # ── Merge fresh data ───────────────────────────────────────────────────────
    for fid, meta in flight_meta.items():
        new_manifest[fid] = {**new_manifest.get(fid, {}), **meta, "missing_runs": 0}

    for fid, quote in quotes_by_id.items():
        entry = {**new_manifest.get(fid, {}), "price": quote.price, "missing_runs": 0}
        if quote.search_url:
            entry["search_url"] = quote.search_url
        new_manifest[fid] = entry

    new_manifest["__updated__"] = updated_at
    return ReconcileResult(manifest=new_manifest, removed=removed)
