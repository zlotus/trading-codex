from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic

from trading_codex.backtest.fixed_snapshot import (
    DAILY_COLUMNS,
    EXECUTION_ADJUSTMENT_FLAG,
    SIGNAL_ADJUSTMENT_FLAG,
    _date_range,
    _instrument_rule,
    _pair_series,
    _PairedDailySeries,
)
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.point_in_time import (
    DEFAULT_BENCHMARK_CODE,
    INDEX_MEMBER_COUNTS,
    PointInTimeCoverageReport,
    assess_point_in_time_coverage,
    filter_memberships_to_listing_window,
)
from trading_codex.data.time import SHANGHAI, require_aware
from trading_codex.domain.models import (
    DecisionPoint,
    DecisionSnapshot,
    InstrumentRule,
    PortfolioPosition,
    SnapshotValidationError,
)

POINT_IN_TIME_DATA_VERSION = "point-in-time-index-universe-eod-v1"


@dataclass(frozen=True)
class PointInTimeDescriptor:
    start_date: date
    end_date: date
    trading_days: tuple[date, ...]
    index_codes: tuple[str, ...]
    benchmark_code: str
    unique_member_codes: tuple[str, ...]
    daily_rows_by_adjustment: dict[str, int]
    benchmark_rows: int
    source_payloads: tuple[str, ...]
    source_payloads_sha256: str
    coverage: PointInTimeCoverageReport
    load_seconds: float


class PointInTimeEodView:
    """Daily point-in-time index universe with a provider index benchmark."""

    def __init__(
        self,
        normalized_root: Path,
        *,
        as_of: datetime,
        start_date: date,
        end_date: date,
        benchmark_code: str = DEFAULT_BENCHMARK_CODE,
        expected_index_counts: dict[str, int] | None = None,
    ) -> None:
        started = monotonic()
        self.normalized_root = normalized_root
        self.as_of = require_aware(as_of, field="as_of")
        self.start_date = start_date
        self.end_date = end_date
        self.benchmark_code = benchmark_code
        self.expected_index_counts = expected_index_counts or INDEX_MEMBER_COUNTS
        store = ParquetDataStore(normalized_root)
        coverage = assess_point_in_time_coverage(
            store,
            start_date=start_date,
            end_date=end_date,
            as_of=self.as_of,
            benchmark_code=benchmark_code,
            expected_index_counts=expected_index_counts,
        )
        if coverage.status != "passed":
            failures = {name: count for name, count in coverage.issue_counts.items() if count}
            raise SnapshotValidationError(f"point-in-time coverage failed: {failures}")

        calendar = store.scan(
            "trade_calendar",
            as_of=self.as_of,
            columns=("calendar_date", "is_trading_day"),
            ranges={"calendar_date": (start_date, end_date)},
        ).to_pylist()
        calendar_by_date = {row["calendar_date"]: row["is_trading_day"] for row in calendar}
        expected_dates = _date_range(start_date, end_date)
        self.trading_days = tuple(day for day in expected_dates if calendar_by_date[day])

        memberships = store.scan(
            "index_memberships",
            as_of=self.as_of,
            columns=("snapshot_date", "index_code", "member_code", "member_name"),
            contained_in={"index_code": tuple(self.expected_index_counts)},
            ranges={"snapshot_date": (start_date, end_date)},
        )
        raw_member_codes = tuple(sorted(set(memberships["member_code"].to_pylist())))
        instruments = store.scan(
            "instruments",
            as_of=self.as_of,
            columns=("code", "ipo_date", "out_date"),
            contained_in={"code": raw_member_codes},
        )
        instrument_codes = set(instruments["code"].to_pylist())
        if instrument_codes != set(raw_member_codes):
            missing = sorted(set(raw_member_codes) - instrument_codes)
            raise SnapshotValidationError(f"point-in-time instruments are missing: {missing[:5]}")
        listed_pairs, _ = filter_memberships_to_listing_window(
            memberships.select(["snapshot_date", "member_code"]).rename_columns(
                ["trade_date", "code"]
            ),
            instruments,
        )
        by_day: dict[date, set[str]] = {day: set() for day in self.trading_days}
        codes: set[str] = set()
        for row in listed_pairs.to_pylist():
            if row["trade_date"] not in by_day:
                continue
            by_day[row["trade_date"]].add(row["code"])
            codes.add(row["code"])
        self._candidates = {day: tuple(sorted(by_day[day])) for day in self.trading_days}
        self.codes = tuple(sorted(codes))
        self._rules: dict[str, InstrumentRule] = {
            code: _instrument_rule(code) for code in self.codes
        }

        universe = store.scan(
            "historical_universe",
            as_of=self.as_of,
            columns=("snapshot_date", "code", "trade_status"),
            contained_in={"code": self.codes},
            ranges={"snapshot_date": (start_date, end_date)},
        ).to_pylist()
        universe_by_day: dict[date, dict[str, bool]] = {
            day: {} for day in self.trading_days
        }
        for row in universe:
            universe_by_day[row["snapshot_date"]][row["code"]] = row["trade_status"]
        self._universe = universe_by_day

        daily = store.daily_bar_series(
            codes=self.codes,
            start_date=start_date,
            end_date=end_date,
            as_of=self.as_of,
            adjustment_flags=(SIGNAL_ADJUSTMENT_FLAG, EXECUTION_ADJUSTMENT_FLAG),
            columns=DAILY_COLUMNS,
        )
        self._series: dict[str, _PairedDailySeries] = {}
        daily_rows_by_adjustment = {
            SIGNAL_ADJUSTMENT_FLAG: 0,
            EXECUTION_ADJUSTMENT_FLAG: 0,
        }
        for code in self.codes:
            signal = daily.get((code, SIGNAL_ADJUSTMENT_FLAG))
            execution = daily.get((code, EXECUTION_ADJUSTMENT_FLAG))
            if signal is None or execution is None:
                raise SnapshotValidationError(f"point-in-time price track is missing for {code}")
            self._series[code] = _pair_series(code, signal, execution)
            daily_rows_by_adjustment[SIGNAL_ADJUSTMENT_FLAG] += signal.num_rows
            daily_rows_by_adjustment[EXECUTION_ADJUSTMENT_FLAG] += execution.num_rows

        for day, candidates in self._candidates.items():
            statuses = self._universe[day]
            for code in candidates:
                index = self._series[code].index_on(day)
                if index is None:
                    raise SnapshotValidationError(
                        f"point-in-time daily row is missing for {day}:{code}"
                    )
                if statuses[code] != self._series[code].bars[index].trade_status:
                    raise SnapshotValidationError(
                        f"point-in-time trade status disagrees for {day}:{code}"
                    )

        benchmark = store.scan(
            "daily_bars",
            as_of=self.as_of,
            columns=("trade_date", "pct_change"),
            equal={"code": benchmark_code, "adjustment_flag": "3"},
            ranges={"trade_date": (start_date, end_date)},
        ).to_pylist()
        self._benchmark_returns = {
            row["trade_date"]: row["pct_change"] / Decimal(100) for row in benchmark
        }

        self.descriptor = PointInTimeDescriptor(
            start_date=start_date,
            end_date=end_date,
            trading_days=self.trading_days,
            index_codes=tuple(self.expected_index_counts),
            benchmark_code=benchmark_code,
            unique_member_codes=self.codes,
            daily_rows_by_adjustment=daily_rows_by_adjustment,
            benchmark_rows=len(benchmark),
            source_payloads=coverage.source_payloads,
            source_payloads_sha256=coverage.source_payloads_sha256,
            coverage=coverage,
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
            raise SnapshotValidationError("snapshot as_of exceeds loaded point-in-time boundary")
        if boundary.astimezone(SHANGHAI).date() != decision_date:
            raise SnapshotValidationError("EOD snapshot as_of must match decision_date")
        if decision_date not in self._candidates:
            raise SnapshotValidationError("decision_date is outside the point-in-time grid")
        if priced_observations < 2:
            raise ValueError("priced_observations must be at least two")

        candidates = self._candidates[decision_date]
        position_codes = {position.code for position in positions}
        unknown_positions = sorted(position_codes - set(self.codes))
        if unknown_positions:
            raise SnapshotValidationError(
                f"positions never occur in the point-in-time universe: {unknown_positions}"
            )
        required_codes = tuple(sorted(set(candidates) | position_codes))
        missing_current = [
            code for code in required_codes if self._series[code].index_on(decision_date) is None
        ]
        if missing_current:
            raise SnapshotValidationError(
                f"point-in-time positions lack current prices: {missing_current}"
            )
        statuses = self._universe[decision_date]
        missing_statuses = [code for code in required_codes if code not in statuses]
        if missing_statuses:
            raise SnapshotValidationError(
                f"point-in-time positions lack current universe state: {missing_statuses}"
            )
        status_disagreements = []
        for code in required_codes:
            index = self._series[code].index_on(decision_date)
            assert index is not None
            if statuses[code] != self._series[code].bars[index].trade_status:
                status_disagreements.append(code)
        if status_disagreements:
            raise SnapshotValidationError(
                "point-in-time position trade status disagrees: "
                f"{status_disagreements}"
            )
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
            candidate_codes=candidates,
            bars=bars,
            positions=tuple(sorted(positions, key=lambda item: item.code)),
            rules=tuple(self._rules[code] for code in required_codes),
            source_payloads=self.descriptor.source_payloads,
            decision_point=DecisionPoint.EOD,
            regime_codes=candidates,
            data_version=POINT_IN_TIME_DATA_VERSION,
        )

    def benchmark_return(self, day: date) -> Decimal:
        try:
            return self._benchmark_returns[day]
        except KeyError as exc:
            raise SnapshotValidationError(f"benchmark row is missing on {day}") from exc
