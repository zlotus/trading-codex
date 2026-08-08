from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from trading_codex.data.cached_client import CachedBaoStockClient
from trading_codex.data.models import (
    CacheMissError,
    ProviderBatch,
    ProviderError,
    RequestBudgetExceeded,
)
from trading_codex.data.raw_store import ImmutableRawStore

RECEIVED_AT = datetime(2024, 1, 3, tzinfo=UTC)


def _batch(operation: str, query: dict[str, str]) -> ProviderBatch:
    return ProviderBatch(
        source="baostock",
        operation=operation,
        query=query,
        fields=("calendar_date", "is_trading_day"),
        rows=({"calendar_date": "2024-01-02", "is_trading_day": "1"},),
        received_at=RECEIVED_AT,
    )


class Upstream:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.requests = 0

    def __enter__(self) -> "Upstream":
        self.entered += 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.exited += 1

    def trade_calendar(self, *, start_date: date, end_date: date) -> ProviderBatch:
        self.requests += 1
        return _batch(
            "trade_calendar",
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )

    def instruments(self, *, code: str = "") -> ProviderBatch:
        self.requests += 1
        return ProviderBatch(
            source="baostock",
            operation="instruments",
            query={"code": code},
            fields=("code",),
            rows=({"code": "sh.600000"},),
            received_at=RECEIVED_AT,
        )

    def daily_bars(
        self,
        *,
        code: str,
        start_date: date,
        end_date: date,
        adjustment_flag: str = "3",
    ) -> ProviderBatch:
        self.requests += 1
        query = {
            "code": code,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "frequency": "d",
            "adjustflag": adjustment_flag,
        }
        return ProviderBatch(
            source="baostock",
            operation="daily_bars",
            query=query,
            fields=("date", "adjustflag"),
            rows=({"date": start_date.isoformat(), "adjustflag": adjustment_flag},),
            received_at=RECEIVED_AT,
        )


def test_network_is_disabled_by_default_on_cache_miss(tmp_path: Path) -> None:
    upstream = Upstream()
    client = CachedBaoStockClient(
        ImmutableRawStore(tmp_path / "raw"), upstream=upstream  # type: ignore[arg-type]
    )

    with client, pytest.raises(CacheMissError):
        client.trade_calendar(start_date=date(2024, 1, 1), end_date=date(2024, 1, 2))

    assert upstream.entered == 0
    assert upstream.requests == 0
    assert client.cache_misses == 1


def test_first_fetch_populates_cache_and_later_read_stays_offline(tmp_path: Path) -> None:
    raw = ImmutableRawStore(tmp_path / "raw")
    upstream = Upstream()
    online = CachedBaoStockClient(
        raw,
        upstream=upstream,  # type: ignore[arg-type]
        allow_network=True,
        max_upstream_requests=1,
        minimum_request_interval=0,
    )
    with online:
        fetched = online.trade_calendar(
            start_date=date(2024, 1, 1), end_date=date(2024, 1, 2)
        )

    offline_upstream = Upstream()
    offline = CachedBaoStockClient(
        raw, upstream=offline_upstream  # type: ignore[arg-type]
    )
    with offline:
        cached = offline.trade_calendar(
            start_date=date(2024, 1, 1), end_date=date(2024, 1, 2)
        )

    assert cached == fetched
    assert online.upstream_requests == 1
    assert offline.cache_hits == 1
    assert offline_upstream.entered == 0
    assert offline_upstream.requests == 0


def test_adjustment_flag_has_an_exact_cache_key_and_replays_offline(tmp_path: Path) -> None:
    raw = ImmutableRawStore(tmp_path / "raw")
    upstream = Upstream()
    online = CachedBaoStockClient(
        raw,
        upstream=upstream,  # type: ignore[arg-type]
        allow_network=True,
        max_upstream_requests=1,
        minimum_request_interval=0,
    )
    with online:
        fetched = online.daily_bars(
            code="sh.600000",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 2),
            adjustment_flag="2",
        )

    offline_upstream = Upstream()
    offline = CachedBaoStockClient(raw, upstream=offline_upstream)  # type: ignore[arg-type]
    with offline:
        cached = offline.daily_bars(
            code="sh.600000",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 2),
            adjustment_flag="2",
        )
        with pytest.raises(CacheMissError, match="adjustflag=3"):
            offline.daily_bars(
                code="sh.600000",
                start_date=date(2024, 1, 2),
                end_date=date(2024, 1, 2),
                adjustment_flag="3",
            )

    assert cached == fetched
    assert online.upstream_requests == 1
    assert offline.cache_hits == 1
    assert offline.cache_misses == 1
    assert offline_upstream.entered == 0
    assert offline_upstream.requests == 0


def test_explicit_request_budget_blocks_second_cache_miss(tmp_path: Path) -> None:
    upstream = Upstream()
    client = CachedBaoStockClient(
        ImmutableRawStore(tmp_path / "raw"),
        upstream=upstream,  # type: ignore[arg-type]
        allow_network=True,
        max_upstream_requests=1,
        minimum_request_interval=0,
    )

    with client:
        client.instruments()
        with pytest.raises(RequestBudgetExceeded):
            client.trade_calendar(
                start_date=date(2024, 1, 1), end_date=date(2024, 1, 2)
            )

    assert upstream.requests == 1
    assert client.upstream_requests == 1
    assert client.cache_misses == 2


def test_request_budget_cannot_exceed_safety_cap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="safety cap"):
        CachedBaoStockClient(
            ImmutableRawStore(tmp_path / "raw"),
            max_upstream_requests=2,
        )


class FailingUpstream(Upstream):
    def trade_calendar(self, *, start_date: date, end_date: date) -> ProviderBatch:
        self.requests += 1
        raise ProviderError("simulated provider failure")


def test_failed_request_attempt_consumes_the_budget(tmp_path: Path) -> None:
    upstream = FailingUpstream()
    client = CachedBaoStockClient(
        ImmutableRawStore(tmp_path / "raw"),
        upstream=upstream,  # type: ignore[arg-type]
        allow_network=True,
        max_upstream_requests=1,
        minimum_request_interval=0,
    )

    with client:
        with pytest.raises(ProviderError, match="simulated"):
            client.trade_calendar(
                start_date=date(2024, 1, 1), end_date=date(2024, 1, 2)
            )
        with pytest.raises(RequestBudgetExceeded):
            client.trade_calendar(
                start_date=date(2024, 1, 1), end_date=date(2024, 1, 2)
            )

    assert upstream.requests == 1
    assert client.upstream_requests == 1


def test_lookup_rebuilds_missing_query_index_without_network(tmp_path: Path) -> None:
    raw = ImmutableRawStore(tmp_path / "raw")
    batch = _batch(
        "trade_calendar",
        {"start_date": "2024-01-01", "end_date": "2024-01-02"},
    )
    raw.persist(batch)
    for index in (tmp_path / "raw" / ".query-cache").rglob("*.json"):
        index.unlink()

    cached = raw.lookup(
        source="baostock", operation=batch.operation, query=batch.query
    )

    assert cached == batch
    assert list((tmp_path / "raw" / ".query-cache").rglob("*.json"))
