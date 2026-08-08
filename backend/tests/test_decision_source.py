from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_codex.data.decision_source import ParquetDecisionSnapshotSource
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import InstrumentRule, SnapshotValidationError

CODE = "sh.600000"


def _provenance(day: date, *, payload: str, available_hour: int = 7) -> dict[str, object]:
    return {
        "available_at": datetime(day.year, day.month, day.day, available_hour, tzinfo=UTC),
        "source": "fixture",
        "source_received_at": datetime(day.year, day.month, day.day, 8, tzinfo=UTC),
        "source_payload_sha256": payload,
        "raw_artifact": f"fixture/{payload}.json",
    }


def _populate_prerequisites(store: ParquetDataStore, days: tuple[date, ...]) -> None:
    calendar_hash = canonical_sha256({"dataset": "calendar"})
    universe_hash = canonical_sha256({"dataset": "universe"})
    store.merge(
        "trade_calendar",
        (
            {
                "calendar_date": day,
                "is_trading_day": True,
                **_provenance(day, payload=calendar_hash, available_hour=0),
            }
            for day in days
        ),
    )
    store.merge(
        "historical_universe",
        (
            {
                "snapshot_date": day,
                "code": CODE,
                "name": "fixture",
                "trade_status": True,
                **_provenance(day, payload=universe_hash, available_hour=1),
            }
            for day in days
        ),
    )


def _daily_row(
    day: date,
    *,
    adjustment_flag: str,
    close: Decimal,
    available_hour: int = 7,
) -> dict[str, object]:
    payload = canonical_sha256(
        {"dataset": "daily", "day": day, "adjustment_flag": adjustment_flag}
    )
    return {
        "trade_date": day,
        "code": CODE,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "previous_close": close - Decimal("0.10"),
        "volume": 100_000,
        "amount": close * 100_000,
        "adjustment_flag": adjustment_flag,
        "turnover": Decimal("0.01"),
        "trade_status": True,
        "pct_change": Decimal("0.01"),
        "is_st": False,
        **_provenance(day, payload=payload, available_hour=available_hour),
    }


def _source(tmp_path: Path) -> tuple[ParquetDataStore, ParquetDecisionSnapshotSource]:
    store = ParquetDataStore(tmp_path / "normalized")
    return store, ParquetDecisionSnapshotSource(store)


def _build(source: ParquetDecisionSnapshotSource, days: tuple[date, ...]):
    return source.build(
        as_of=datetime(2024, 1, 4, 8, tzinfo=UTC),
        history_start=days[0],
        decision_date=days[-1],
        execution_deadline=datetime(2024, 1, 5, 1, 35, tzinfo=UTC),
        cash=Decimal("100000"),
        candidate_codes=(CODE,),
        rules=(
            InstrumentRule(
                code=CODE,
                lot_size=100,
                price_limit_ratio=Decimal("0.10"),
            ),
        ),
    )


def test_snapshot_source_pairs_signal_and_execution_prices(tmp_path: Path) -> None:
    store, source = _source(tmp_path)
    days = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    _populate_prerequisites(store, days)
    store.merge(
        "daily_bars",
        (
            _daily_row(day, adjustment_flag=flag, close=Decimal(index + offset))
            for index, day in enumerate(days, start=10)
            for flag, offset in (("2", 0), ("3", 10))
        ),
    )

    snapshot = _build(source, days)

    current = snapshot.state_on(CODE, days[-1])
    assert current is not None
    assert current.signal_close == Decimal("12")
    assert current.execution_close == Decimal("22")
    assert current.previous_close == Decimal("21.90")
    assert snapshot.snapshot_id == snapshot.snapshot_id
    assert len(snapshot.source_payloads) == 8


def test_snapshot_source_fails_when_either_price_track_is_missing(tmp_path: Path) -> None:
    store, source = _source(tmp_path)
    days = (date(2024, 1, 4),)
    _populate_prerequisites(store, days)
    store.merge(
        "daily_bars",
        (_daily_row(days[0], adjustment_flag="3", close=Decimal("10")),),
    )

    with pytest.raises(SnapshotValidationError, match="price tracks"):
        _build(source, days)


def test_snapshot_source_rejects_price_unavailable_at_as_of(tmp_path: Path) -> None:
    store, source = _source(tmp_path)
    days = (date(2024, 1, 4),)
    _populate_prerequisites(store, days)
    store.merge(
        "daily_bars",
        (
            _daily_row(days[0], adjustment_flag="3", close=Decimal("10")),
            _daily_row(
                days[0],
                adjustment_flag="2",
                close=Decimal("9"),
                available_hour=9,
            ),
        ),
    )

    with pytest.raises(SnapshotValidationError, match="price tracks"):
        _build(source, days)


def test_snapshot_source_requires_complete_calendar_and_historical_universe(
    tmp_path: Path,
) -> None:
    store, source = _source(tmp_path)
    days = (date(2024, 1, 3), date(2024, 1, 4))
    _populate_prerequisites(store, (days[-1],))

    with pytest.raises(SnapshotValidationError, match="calendar is incomplete"):
        _build(source, days)

    calendar_hash = canonical_sha256({"dataset": "calendar-extra"})
    store.merge(
        "trade_calendar",
        (
            {
                "calendar_date": days[0],
                "is_trading_day": True,
                **_provenance(days[0], payload=calendar_hash, available_hour=0),
            },
        ),
    )
    with pytest.raises(SnapshotValidationError, match="universe is incomplete"):
        _build(source, days)
