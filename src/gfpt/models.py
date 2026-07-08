"""Typed domain models shared across the package."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TelegramUser:
    """One authorized bot user. chat_id is Telegram's numeric ID as a string."""
    chat_id: str
    name: str


@dataclass(frozen=True)
class PriceQuote:
    """One flight price extracted from a GetSolutionPrices response."""
    flight_id: str
    price: int
    search_url: str | None = None


@dataclass(frozen=True)
class RunSummary:
    """Outcome of one tracker run, persisted to S3 for /status."""
    ok: bool
    flights: int = 0
    updated: int = 0
    removed: int = 0
    runtime_secs: float = 0.0
    finished_at: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "flights": self.flights,
            "updated": self.updated,
            "removed": self.removed,
            "runtime_secs": round(self.runtime_secs, 1),
            "finished_at": self.finished_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> RunSummary | None:
        if not isinstance(data, dict):
            return None
        return cls(
            ok=bool(data.get("ok")),
            flights=int(data.get("flights", 0)),
            updated=int(data.get("updated", 0)),
            removed=int(data.get("removed", 0)),
            runtime_secs=float(data.get("runtime_secs", 0.0)),
            finished_at=str(data.get("finished_at", data.get("ts", ""))),
            error=str(data.get("error", "")),
        )


@dataclass(frozen=True)
class ReconcileResult:
    """Result of merging fresh scrape data into the flights manifest."""
    manifest: dict
    removed: dict = field(default_factory=dict)  # flight_id -> last-known meta
