from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from trading_codex.backtest.fixed_snapshot import (
    FIXED_SNAPSHOT_DATA_VERSION,
    FixedSnapshotEodView,
)
from trading_codex.backtest.m8_smoke import (
    DEFAULT_PARAMETERS,
    _code_descriptor,
    _pipeline_config,
)
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.domain.models import DecisionPoint
from trading_codex.domain.pipeline import DecisionPipeline

AS_OF = datetime(2024, 2, 9, 7, tzinfo=UTC)
CODES = tuple(f"sh.6000{index:02d}" for index in range(5))


def _provenance(payload: str, available_at: datetime) -> dict[str, object]:
    return {
        "available_at": available_at,
        "source": "fixture",
        "source_received_at": AS_OF,
        "source_payload_sha256": payload * 64,
        "raw_artifact": f"fixture/{payload}.json",
    }


def _build_store(root: Path) -> tuple[ParquetDataStore, tuple[date, ...]]:
    store = ParquetDataStore(root)
    start = date(2024, 1, 1)
    days = tuple(start + timedelta(days=offset) for offset in range(40))
    trading_days = tuple(day for day in days if day.weekday() < 5)
    universe_date = trading_days[0]
    store.merge(
        "trade_calendar",
        (
            {
                "calendar_date": day,
                "is_trading_day": day in trading_days,
                **_provenance(
                    "a",
                    datetime.combine(day, time.min, tzinfo=UTC),
                ),
            }
            for day in days
        ),
    )
    store.merge(
        "index_memberships",
        (
            {
                "snapshot_date": universe_date,
                "index_code": "sh.000300" if index < 3 else "sh.000905",
                "member_code": code,
                "member_name": f"Fixture {index}",
                **_provenance("b", datetime(2024, 1, 1, 1, tzinfo=UTC)),
            }
            for index, code in enumerate(CODES)
        ),
    )
    store.merge(
        "instruments",
        (
            {
                "code": code,
                "name": f"Fixture {index}",
                "ipo_date": date(2000, 1, 1),
                "out_date": None,
                "security_type": "1",
                "status": "1",
                **_provenance("c", datetime(2000, 1, 1, tzinfo=UTC)),
            }
            for index, code in enumerate(CODES)
        ),
    )
    store.merge(
        "daily_bars",
        (
            _daily_row(code, day, day_index, adjustment_flag)
            for code_index, code in enumerate(CODES)
            for day_index, day in enumerate(trading_days)
            for adjustment_flag in ("2", "3")
        ),
    )
    return store, trading_days


def _daily_row(
    code: str,
    day: date,
    day_index: int,
    adjustment_flag: str,
) -> dict[str, object]:
    offset = Decimal("0.5") if adjustment_flag == "3" else Decimal(0)
    close = Decimal("10") + Decimal(day_index) / Decimal(10) + offset
    previous = close - Decimal("0.09")
    return {
        "trade_date": day,
        "code": code,
        "open": close - Decimal("0.02"),
        "high": close + Decimal("0.05"),
        "low": close - Decimal("0.05"),
        "close": close,
        "previous_close": previous,
        "volume": 100_000,
        "amount": close * 100_000,
        "adjustment_flag": adjustment_flag,
        "turnover": Decimal("0.5"),
        "trade_status": True,
        "pct_change": Decimal("0.01"),
        "is_st": False,
        **_provenance(
            "d" if adjustment_flag == "2" else "e",
            datetime.combine(day, time(7), tzinfo=UTC),
        ),
    }


def test_fixed_snapshot_view_pairs_tracks_and_reuses_a_bounded_window(
    tmp_path: Path,
) -> None:
    _, trading_days = _build_store(tmp_path / "normalized")
    view = FixedSnapshotEodView(
        tmp_path / "normalized",
        as_of=AS_OF,
        universe_date=trading_days[0],
        data_start=date(2024, 1, 1),
        data_end=date(2024, 2, 9),
    )

    snapshot = view.snapshot(
        decision_date=trading_days[-1],
        as_of=AS_OF,
        cash=Decimal("1000000"),
        priced_observations=21,
    )

    assert snapshot.data_version == FIXED_SNAPSHOT_DATA_VERSION
    assert snapshot.decision_point is DecisionPoint.EOD
    assert snapshot.candidate_codes == CODES
    assert snapshot.regime_codes == CODES
    assert snapshot.opening_bars == ()
    assert len(snapshot.bars) == 21 * len(CODES)
    current = snapshot.state_on(CODES[0], trading_days[-1])
    assert current is not None
    assert current.execution_close - current.signal_close == Decimal("0.500000")
    assert view.descriptor.daily_rows_by_adjustment == {"2": 150, "3": 150}
    assert view.descriptor.source_payloads_sha256
    assert view.benchmark_return(trading_days[-1]) > 0

    run = DecisionPipeline(_pipeline_config(DEFAULT_PARAMETERS[-1])).run(snapshot)
    assert run.regime.version == "interpretable-market-regime-eod-v1"
    assert run.regime.features.opening_coverage == 0
    assert "opening_feature=disabled_eod" in run.regime.explanations
    assert run.allocated.decision_point is DecisionPoint.EOD

    code = _code_descriptor()
    assert len(code["git_commit"]) == 40
    assert len(code["python_source_sha256"]) == 64
    assert code["hashed_files"] > 10
