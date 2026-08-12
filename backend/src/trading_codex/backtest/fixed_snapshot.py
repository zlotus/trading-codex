from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.time import SHANGHAI, require_aware
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import (
    DailyBar,
    DecisionPoint,
    DecisionSnapshot,
    InstrumentRule,
    PortfolioPosition,
    SnapshotValidationError,
)

FIXED_SNAPSHOT_DATA_VERSION = "fixed-snapshot-universe-eod-smoke-v1"
SIGNAL_ADJUSTMENT_FLAG = "2"
EXECUTION_ADJUSTMENT_FLAG = "3"
DEFAULT_INDEX_CODES = ("sh.000300", "sh.000905")
DAILY_COLUMNS = (
    "trade_date",
    "code",
    "close",
    "previous_close",
    "volume",
    "amount",
    "adjustment_flag",
    "turnover",
    "trade_status",
    "is_st",
    "available_at",
    "source_payload_sha256",
)


@dataclass(frozen=True)
class FixedSnapshotDescriptor:
    universe_date: date
    index_codes: tuple[str, ...]
    codes: tuple[str, ...]
    data_start: date
    data_end: date
    trading_days: tuple[date, ...]
    daily_rows_by_adjustment: dict[str, int]
    source_payloads: tuple[str, ...]
    load_seconds: float

    @property
    def source_payloads_sha256(self) -> str:
        return canonical_sha256({"source_payloads": self.source_payloads})


@dataclass(frozen=True)
class _PairedDailySeries:
    dates: np.ndarray
    bars: tuple[DailyBar, ...]
    priced_indices: np.ndarray

    def index_on(self, day: date) -> int | None:
        target = np.datetime64(day, "D")
        index = int(np.searchsorted(self.dates, target, side="left"))
        if index >= len(self.dates) or self.dates[index] != target:
            return None
        return index

    def window(self, day: date, *, priced_observations: int) -> tuple[DailyBar, ...]:
        current = self.index_on(day)
        if current is None:
            return ()
        priced_end = int(np.searchsorted(self.priced_indices, current, side="right"))
        selected = self.priced_indices[max(0, priced_end - priced_observations) : priced_end]
        indices = selected.tolist()
        if not indices or indices[-1] != current:
            indices.append(current)
        return tuple(self.bars[index] for index in sorted(set(indices)))


class FixedSnapshotEodView:
    """Bounded, survivorship-biased EOD view for the M8.1 engineering smoke."""

    def __init__(
        self,
        normalized_root: Path,
        *,
        as_of: datetime,
        universe_date: date,
        data_start: date,
        data_end: date,
        index_codes: tuple[str, ...] = DEFAULT_INDEX_CODES,
    ) -> None:
        started = monotonic()
        self.normalized_root = normalized_root
        self.as_of = require_aware(as_of, field="as_of")
        self.universe_date = universe_date
        self.data_start = data_start
        self.data_end = data_end
        self.index_codes = tuple(sorted(set(index_codes)))
        if not self.index_codes:
            raise ValueError("at least one fixed-snapshot index code is required")
        if data_end < data_start:
            raise ValueError("data_end must not precede data_start")
        if data_end > self.as_of.astimezone(SHANGHAI).date():
            raise SnapshotValidationError("fixed-snapshot data_end exceeds as_of")
        if not data_start <= universe_date <= data_end:
            raise SnapshotValidationError("universe_date must be inside the loaded range")

        store = ParquetDataStore(normalized_root)
        membership_rows = store.scan(
            "index_memberships",
            as_of=self.as_of,
            columns=(
                "snapshot_date",
                "index_code",
                "member_code",
                "member_name",
                "source_payload_sha256",
            ),
            equal={"snapshot_date": universe_date},
            contained_in={"index_code": self.index_codes},
        ).to_pylist()
        observed_indexes = {row["index_code"] for row in membership_rows}
        if observed_indexes != set(self.index_codes):
            missing = sorted(set(self.index_codes) - observed_indexes)
            raise SnapshotValidationError(f"fixed index snapshots are missing: {missing}")
        names: dict[str, str] = {}
        for row in membership_rows:
            existing = names.setdefault(row["member_code"], row["member_name"])
            if existing != row["member_name"]:
                raise SnapshotValidationError(
                    f"fixed universe names disagree for {row['member_code']}"
                )
        self.codes = tuple(sorted(names))
        if not self.codes:
            raise SnapshotValidationError("fixed snapshot universe is empty")

        instrument_rows = store.scan(
            "instruments",
            as_of=self.as_of,
            columns=("code", "source_payload_sha256"),
            contained_in={"code": self.codes},
        ).to_pylist()
        instrument_codes = {row["code"] for row in instrument_rows}
        if instrument_codes != set(self.codes):
            missing = sorted(set(self.codes) - instrument_codes)
            raise SnapshotValidationError(f"fixed universe instruments are missing: {missing}")
        self._rules = {code: _instrument_rule(code) for code in self.codes}

        calendar_rows = store.scan(
            "trade_calendar",
            as_of=self.as_of,
            columns=("calendar_date", "is_trading_day", "source_payload_sha256"),
            ranges={"calendar_date": (data_start, data_end)},
        ).to_pylist()
        calendar_by_date = {row["calendar_date"]: row for row in calendar_rows}
        expected_dates = _date_range(data_start, data_end)
        missing_calendar = sorted(set(expected_dates) - set(calendar_by_date))
        if missing_calendar:
            sample = [value.isoformat() for value in missing_calendar[:5]]
            raise SnapshotValidationError(f"trade calendar is incomplete: {sample}")
        trading_days = tuple(
            day
            for day in expected_dates
            if calendar_by_date[day]["is_trading_day"] and day >= universe_date
        )
        if not trading_days:
            raise SnapshotValidationError("fixed-snapshot interval has no trading days")

        daily = store.daily_bar_series(
            codes=self.codes,
            start_date=data_start,
            end_date=data_end,
            as_of=self.as_of,
            adjustment_flags=(SIGNAL_ADJUSTMENT_FLAG, EXECUTION_ADJUSTMENT_FLAG),
            columns=DAILY_COLUMNS,
        )
        self._series: dict[str, _PairedDailySeries] = {}
        daily_rows_by_adjustment = {
            SIGNAL_ADJUSTMENT_FLAG: 0,
            EXECUTION_ADJUSTMENT_FLAG: 0,
        }
        daily_payloads: set[str] = set()
        for code in self.codes:
            signal = daily.get((code, SIGNAL_ADJUSTMENT_FLAG))
            execution = daily.get((code, EXECUTION_ADJUSTMENT_FLAG))
            if signal is None or execution is None:
                raise SnapshotValidationError(f"daily price track is missing for {code}")
            paired = _pair_series(code, signal, execution)
            self._series[code] = paired
            daily_rows_by_adjustment[SIGNAL_ADJUSTMENT_FLAG] += signal.num_rows
            daily_rows_by_adjustment[EXECUTION_ADJUSTMENT_FLAG] += execution.num_rows
            daily_payloads.update(pc.unique(signal["source_payload_sha256"]).to_pylist())
            daily_payloads.update(pc.unique(execution["source_payload_sha256"]).to_pylist())

        metadata_payloads = {
            *(row["source_payload_sha256"] for row in membership_rows),
            *(row["source_payload_sha256"] for row in instrument_rows),
            *(row["source_payload_sha256"] for row in calendar_rows),
        }
        source_payloads = tuple(sorted(daily_payloads | metadata_payloads))
        self.descriptor = FixedSnapshotDescriptor(
            universe_date=universe_date,
            index_codes=self.index_codes,
            codes=self.codes,
            data_start=data_start,
            data_end=data_end,
            trading_days=trading_days,
            daily_rows_by_adjustment=daily_rows_by_adjustment,
            source_payloads=source_payloads,
            load_seconds=monotonic() - started,
        )

    def snapshot(
        self,
        *,
        decision_date: date,
        as_of: datetime,
        cash: Decimal,
        positions: tuple[PortfolioPosition, ...] = (),
        priced_observations: int = 21,
    ) -> DecisionSnapshot:
        boundary = require_aware(as_of, field="as_of")
        if boundary > self.as_of:
            raise SnapshotValidationError("snapshot as_of exceeds loaded data boundary")
        if boundary.astimezone(SHANGHAI).date() != decision_date:
            raise SnapshotValidationError("EOD snapshot as_of must match decision_date")
        if decision_date not in self.descriptor.trading_days:
            raise SnapshotValidationError("decision_date is outside the loaded trading grid")
        if priced_observations < 2:
            raise ValueError("priced_observations must be at least two")

        position_codes = {position.code for position in positions}
        unknown_positions = sorted(position_codes - set(self.codes))
        if unknown_positions:
            raise SnapshotValidationError(
                f"positions are outside the fixed universe: {unknown_positions}"
            )
        active_codes = tuple(
            code for code in self.codes if self._series[code].index_on(decision_date) is not None
        )
        if not active_codes:
            raise SnapshotValidationError("fixed universe has no rows on decision_date")
        required_codes = tuple(sorted(set(active_codes) | position_codes))
        bars = tuple(
            bar
            for code in required_codes
            for bar in self._series[code].window(
                decision_date,
                priced_observations=priced_observations,
            )
        )
        return DecisionSnapshot(
            as_of=boundary,
            decision_date=decision_date,
            execution_deadline=datetime.combine(
                decision_date + timedelta(days=1),
                time(15),
                tzinfo=SHANGHAI,
            ),
            cash=cash,
            candidate_codes=active_codes,
            bars=bars,
            positions=tuple(sorted(positions, key=lambda item: item.code)),
            rules=tuple(self._rules[code] for code in required_codes),
            source_payloads=self.descriptor.source_payloads,
            decision_point=DecisionPoint.EOD,
            regime_codes=active_codes,
            data_version=FIXED_SNAPSHOT_DATA_VERSION,
        )

    def benchmark_return(self, day: date) -> Decimal:
        returns = []
        for code in self.codes:
            series = self._series[code]
            index = series.index_on(day)
            if index is None:
                continue
            bar = series.bars[index]
            if (
                not bar.trade_status
                or bar.execution_close is None
                or bar.previous_close is None
            ):
                returns.append(Decimal(0))
            else:
                returns.append(bar.execution_close / bar.previous_close - Decimal(1))
        if not returns:
            raise SnapshotValidationError(f"benchmark has no fixed-universe rows on {day}")
        return sum(returns, Decimal(0)) / Decimal(len(returns))


def _pair_series(
    code: str,
    signal: pa.Table,
    execution: pa.Table,
) -> _PairedDailySeries:
    signal_dates = signal["trade_date"].combine_chunks().to_numpy(zero_copy_only=False)
    execution_dates = execution["trade_date"].combine_chunks().to_numpy(zero_copy_only=False)
    if not np.array_equal(signal_dates, execution_dates):
        raise SnapshotValidationError(f"daily price-track dates disagree for {code}")
    for field in ("volume", "trade_status", "is_st"):
        if not pc.all(pc.equal(signal[field], execution[field])).as_py():
            raise SnapshotValidationError(f"daily price tracks disagree for {code}: {field}")

    signal_rows = signal.to_pydict()
    execution_rows = execution.to_pydict()
    bars = tuple(
        DailyBar(
            code=code,
            trade_date=execution_rows["trade_date"][index],
            signal_close=signal_rows["close"][index],
            execution_close=execution_rows["close"][index],
            previous_close=execution_rows["previous_close"][index],
            volume=execution_rows["volume"][index],
            trade_status=execution_rows["trade_status"][index],
            is_st=execution_rows["is_st"][index],
            available_at=max(
                signal_rows["available_at"][index],
                execution_rows["available_at"][index],
            ),
            amount=execution_rows["amount"][index],
            turnover=execution_rows["turnover"][index],
        )
        for index in range(signal.num_rows)
    )
    priced_indices = np.fromiter(
        (
            index
            for index, bar in enumerate(bars)
            if bar.trade_status
            and bar.signal_close is not None
            and bar.execution_close is not None
            and bar.previous_close is not None
        ),
        dtype=np.int64,
    )
    return _PairedDailySeries(
        dates=signal_dates.astype("datetime64[D]", copy=False),
        bars=bars,
        priced_indices=priced_indices,
    )


def _instrument_rule(code: str) -> InstrumentRule:
    if code.startswith("sh.688"):
        return InstrumentRule(code=code, lot_size=200, price_limit_ratio=Decimal("0.20"))
    if code.startswith(("sz.300", "sz.301")):
        return InstrumentRule(code=code, lot_size=100, price_limit_ratio=Decimal("0.20"))
    if code.startswith("bj."):
        return InstrumentRule(code=code, lot_size=100, price_limit_ratio=Decimal("0.30"))
    return InstrumentRule(code=code, lot_size=100, price_limit_ratio=Decimal("0.10"))


def _date_range(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))
