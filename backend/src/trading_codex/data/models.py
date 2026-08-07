from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class MarketDataError(RuntimeError):
    """Base error for market-data ingestion and reads."""


class ProviderError(MarketDataError):
    """The upstream provider rejected or failed a request."""


class CacheMissError(ProviderError):
    """A cache-only provider read could not find the requested response."""


class RequestBudgetExceeded(ProviderError):
    """A provider call would exceed the explicit upstream request budget."""


class DataValidationError(MarketDataError):
    """Provider data is missing required fields or violates invariants."""


class RawIntegrityError(MarketDataError):
    """An immutable raw artifact does not match its content address."""


class FutureDataError(MarketDataError):
    """A point-in-time query requested data beyond its as_of boundary."""


@dataclass(frozen=True)
class ProviderBatch:
    source: str
    operation: str
    query: dict[str, str]
    fields: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    received_at: datetime

    def __post_init__(self) -> None:
        if not self.source or not self.operation:
            raise ValueError("source and operation are required")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        expected = set(self.fields)
        for row in self.rows:
            if set(row) != expected:
                raise ValueError("each provider row must exactly match fields")


@dataclass(frozen=True)
class RawArtifact:
    path: Path
    relative_path: str
    content_sha256: str
    received_at: datetime


@dataclass(frozen=True)
class MergeResult:
    dataset: str
    incoming: int
    inserted: int
    updated: int
    unchanged: int
    total: int

    @property
    def changed(self) -> bool:
        return self.inserted > 0 or self.updated > 0
