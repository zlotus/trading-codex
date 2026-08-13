from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from trading_codex.data.models import FutureDataError
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.time import SHANGHAI, require_aware
from trading_codex.domain.hashing import canonical_sha256

INDEX_MEMBER_COUNTS = {"sh.000300": 300, "sh.000905": 500}
INDEX_CODES = tuple(INDEX_MEMBER_COUNTS)
DEFAULT_BENCHMARK_CODE = "sh.000906"
# BaoStock returns a continuous, one-member CSI 500 vacancy on these exact dates.
ZZ500_VACANCY_DATES = frozenset(
    date.fromisoformat(value)
    for value in (
        "2019-01-07",
        "2019-01-08",
        "2019-01-09",
        "2019-01-10",
        "2019-01-11",
        "2019-01-14",
        "2019-01-15",
        "2019-01-16",
        "2019-01-17",
        "2019-01-18",
        "2021-09-13",
        "2021-09-14",
        "2021-09-15",
        "2021-09-16",
        "2021-09-17",
        "2021-09-22",
        "2021-09-23",
        "2021-09-24",
        "2021-09-27",
        "2021-09-28",
        "2021-09-29",
        "2021-09-30",
    )
)
DECISION_CUTOFF_UTC = timedelta(hours=7, minutes=5)
SAMPLE_LIMIT = 100


@dataclass(frozen=True)
class PointInTimeCoverageReport:
    generated_at: datetime
    as_of: datetime
    status: str
    formal_m4_oos: bool
    start_date: date
    end_date: date
    benchmark_code: str
    trading_days: int
    unique_member_codes: int
    raw_member_code_days: int
    excluded_out_of_listing_member_days: int
    expected_member_code_days: int
    covered_universe_member_days: int
    covered_signal_price_days: int
    covered_execution_price_days: int
    covered_benchmark_days: int
    source_payloads: tuple[str, ...]
    source_payloads_sha256: str
    source_received_at_min: datetime | None
    source_received_at_max: datetime | None
    incomplete_membership_snapshots: tuple[str, ...]
    missing_calendar_dates: tuple[str, ...]
    missing_instruments: tuple[str, ...]
    excluded_out_of_listing_members: tuple[str, ...]
    missing_universe_dates: tuple[str, ...]
    missing_universe_members: tuple[str, ...]
    missing_signal_prices: tuple[str, ...]
    missing_execution_prices: tuple[str, ...]
    price_track_date_mismatches: tuple[str, ...]
    missing_benchmark_dates: tuple[str, ...]
    late_rows: tuple[str, ...]
    trade_status_mismatches: tuple[str, ...]
    issue_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_point_in_time_coverage(
    store: ParquetDataStore,
    *,
    start_date: date,
    end_date: date,
    as_of: datetime,
    benchmark_code: str = DEFAULT_BENCHMARK_CODE,
    generated_at: datetime | None = None,
    expected_index_counts: dict[str, int] | None = None,
) -> PointInTimeCoverageReport:
    boundary = require_aware(as_of, field="as_of")
    generated = require_aware(generated_at or datetime.now(UTC), field="generated_at")
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    if end_date > boundary.astimezone(SHANGHAI).date():
        raise FutureDataError("coverage end_date exceeds as_of")
    expected_counts = expected_index_counts or INDEX_MEMBER_COUNTS
    if not expected_counts or any(value <= 0 for value in expected_counts.values()):
        raise ValueError("expected index membership counts must be positive")

    requested_dates = tuple(
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    )
    calendar = store.scan(
        "trade_calendar",
        as_of=boundary,
        columns=(
            "calendar_date",
            "is_trading_day",
            "source_received_at",
            "source_payload_sha256",
        ),
        ranges={"calendar_date": (start_date, end_date)},
    )
    calendar_rows = calendar.to_pylist()
    calendar_by_date = {row["calendar_date"]: row["is_trading_day"] for row in calendar_rows}
    missing_calendar = tuple(day for day in requested_dates if day not in calendar_by_date)
    trading_days = tuple(day for day in requested_dates if calendar_by_date.get(day) is True)
    trading_day_set = set(trading_days)

    memberships = store.scan(
        "index_memberships",
        as_of=boundary,
        columns=(
            "snapshot_date",
            "index_code",
            "member_code",
            "available_at",
            "source_received_at",
            "source_payload_sha256",
        ),
        contained_in={"index_code": tuple(expected_counts)},
        ranges={"snapshot_date": (start_date, end_date)},
    )
    grouped = _membership_counts(memberships)
    incomplete = tuple(
        f"{day.isoformat()}:{index_code}:{grouped.get((day, index_code), 0)}/{expected}"
        for day in trading_days
        for index_code in expected_counts
        if grouped.get((day, index_code), 0)
        != expected_index_member_count(
            day,
            index_code,
            base_counts=expected_index_counts,
        )
        for expected in (
            expected_index_member_count(
                day,
                index_code,
                base_counts=expected_index_counts,
            ),
        )
    )
    raw_member_pairs = memberships.select(["snapshot_date", "member_code"]).rename_columns(
        ["trade_date", "code"]
    )
    duplicate_member_pairs = _duplicate_member_pair_count(memberships)
    member_codes = (
        tuple(sorted(pc.unique(raw_member_pairs["code"]).to_pylist()))
        if raw_member_pairs.num_rows
        else ()
    )

    instruments = store.scan(
        "instruments",
        as_of=boundary,
        columns=(
            "code",
            "ipo_date",
            "out_date",
            "source_received_at",
            "source_payload_sha256",
        ),
        contained_in={"code": member_codes},
    )
    instrument_codes = set(instruments["code"].to_pylist())
    missing_instruments = tuple(sorted(set(member_codes) - instrument_codes))
    member_pairs, excluded_member_pairs = filter_memberships_to_listing_window(
        raw_member_pairs,
        instruments,
    )

    universe = store.scan(
        "historical_universe",
        as_of=boundary,
        columns=(
            "snapshot_date",
            "code",
            "trade_status",
            "available_at",
            "source_received_at",
            "source_payload_sha256",
        ),
        contained_in={"code": member_codes},
        ranges={"snapshot_date": (start_date, end_date)},
    )
    universe_pairs = universe.select(["snapshot_date", "code"]).rename_columns(
        ["trade_date", "code"]
    )
    missing_universe_pairs = _left_anti(member_pairs, universe_pairs)
    universe_dates = set(pc.unique(universe["snapshot_date"]).to_pylist()) if universe else set()
    missing_universe_dates = tuple(sorted(trading_day_set - universe_dates))

    daily = store.scan(
        "daily_bars",
        as_of=boundary,
        columns=(
            "trade_date",
            "code",
            "adjustment_flag",
            "trade_status",
            "available_at",
            "source_received_at",
            "source_payload_sha256",
        ),
        contained_in={"code": member_codes, "adjustment_flag": ("2", "3")},
        ranges={"trade_date": (start_date, end_date)},
    )
    signal = daily.filter(pc.equal(daily["adjustment_flag"], pa.scalar("2")))
    execution = daily.filter(pc.equal(daily["adjustment_flag"], pa.scalar("3")))
    signal_pairs = signal.select(["trade_date", "code"])
    execution_pairs = execution.select(["trade_date", "code"])
    missing_signal = _left_anti(member_pairs, signal_pairs)
    missing_execution = _left_anti(member_pairs, execution_pairs)
    signal_only = _left_anti(signal_pairs, execution_pairs)
    execution_only = _left_anti(execution_pairs, signal_pairs)
    price_track_date_mismatches = pa.concat_tables((signal_only, execution_only))

    benchmark = store.scan(
        "daily_bars",
        as_of=boundary,
        columns=(
            "trade_date",
            "code",
            "adjustment_flag",
            "close",
            "previous_close",
            "pct_change",
            "available_at",
            "source_received_at",
            "source_payload_sha256",
        ),
        equal={"code": benchmark_code, "adjustment_flag": "3"},
        ranges={"trade_date": (start_date, end_date)},
    )
    valid_benchmark = benchmark.filter(
        pc.and_(
            pc.and_(pc.is_valid(benchmark["close"]), pc.is_valid(benchmark["previous_close"])),
            pc.is_valid(benchmark["pct_change"]),
        )
    )
    benchmark_dates = set(pc.unique(valid_benchmark["trade_date"]).to_pylist())
    missing_benchmark = tuple(sorted(trading_day_set - benchmark_dates))

    late_tables = (
        ("index_memberships", memberships, "snapshot_date"),
        ("historical_universe", universe, "snapshot_date"),
        ("daily_bars", daily, "trade_date"),
        ("benchmark", benchmark, "trade_date"),
    )
    late_rows = tuple(
        f"{name}:{count}"
        for name, table, date_column in late_tables
        if (count := _late_row_count(table, date_column))
    )
    status_mismatches = _trade_status_mismatches(
        member_pairs,
        universe,
        signal,
        execution,
    )

    provenance_tables = (
        calendar,
        memberships,
        instruments,
        universe,
        daily,
        benchmark,
    )
    payloads = tuple(
        sorted(
            {
                payload
                for table in provenance_tables
                if table.num_rows
                for payload in pc.unique(table["source_payload_sha256"]).to_pylist()
            }
        )
    )
    source_received_at_min, source_received_at_max = _received_at_bounds(
        provenance_tables
    )
    issue_counts = {
        "missing_calendar_dates": len(missing_calendar),
        "incomplete_membership_snapshots": len(incomplete),
        "duplicate_member_pairs": duplicate_member_pairs,
        "missing_instruments": len(missing_instruments),
        "missing_universe_dates": len(missing_universe_dates),
        "missing_universe_members": missing_universe_pairs.num_rows,
        "missing_signal_prices": missing_signal.num_rows,
        "missing_execution_prices": missing_execution.num_rows,
        "price_track_date_mismatches": price_track_date_mismatches.num_rows,
        "missing_benchmark_dates": len(missing_benchmark),
        "late_row_groups": len(late_rows),
        "trade_status_mismatches": status_mismatches.num_rows,
    }
    passed = bool(trading_days) and not any(issue_counts.values())
    return PointInTimeCoverageReport(
        generated_at=generated,
        as_of=boundary,
        status="passed" if passed else "failed",
        formal_m4_oos=False,
        start_date=start_date,
        end_date=end_date,
        benchmark_code=benchmark_code,
        trading_days=len(trading_days),
        unique_member_codes=len(member_codes),
        raw_member_code_days=raw_member_pairs.num_rows,
        excluded_out_of_listing_member_days=excluded_member_pairs.num_rows,
        expected_member_code_days=member_pairs.num_rows,
        covered_universe_member_days=member_pairs.num_rows - missing_universe_pairs.num_rows,
        covered_signal_price_days=member_pairs.num_rows - missing_signal.num_rows,
        covered_execution_price_days=member_pairs.num_rows - missing_execution.num_rows,
        covered_benchmark_days=len(benchmark_dates & trading_day_set),
        source_payloads=payloads,
        source_payloads_sha256=canonical_sha256({"source_payloads": payloads}),
        source_received_at_min=source_received_at_min,
        source_received_at_max=source_received_at_max,
        incomplete_membership_snapshots=incomplete[:SAMPLE_LIMIT],
        missing_calendar_dates=tuple(day.isoformat() for day in missing_calendar[:SAMPLE_LIMIT]),
        missing_instruments=missing_instruments[:SAMPLE_LIMIT],
        excluded_out_of_listing_members=_pair_samples(excluded_member_pairs),
        missing_universe_dates=tuple(
            day.isoformat() for day in missing_universe_dates[:SAMPLE_LIMIT]
        ),
        missing_universe_members=_pair_samples(missing_universe_pairs),
        missing_signal_prices=_pair_samples(missing_signal),
        missing_execution_prices=_pair_samples(missing_execution),
        price_track_date_mismatches=_pair_samples(price_track_date_mismatches),
        missing_benchmark_dates=tuple(
            day.isoformat() for day in missing_benchmark[:SAMPLE_LIMIT]
        ),
        late_rows=late_rows,
        trade_status_mismatches=_pair_samples(status_mismatches),
        issue_counts=issue_counts,
    )


def _membership_counts(table: pa.Table) -> dict[tuple[date, str], int]:
    if not table.num_rows:
        return {}
    grouped = table.group_by(["snapshot_date", "index_code"]).aggregate(
        [("member_code", "count_distinct")]
    )
    return {
        (row["snapshot_date"], row["index_code"]): row["member_code_count_distinct"]
        for row in grouped.to_pylist()
    }


def expected_index_member_count(
    snapshot_date: date,
    index_code: str,
    *,
    base_counts: dict[str, int] | None = None,
) -> int:
    counts = base_counts or INDEX_MEMBER_COUNTS
    try:
        expected = counts[index_code]
    except KeyError as exc:
        raise ValueError(f"unsupported index code: {index_code}") from exc
    if (
        base_counts is None
        and index_code == "sh.000905"
        and snapshot_date in ZZ500_VACANCY_DATES
    ):
        return 499
    return expected


def filter_memberships_to_listing_window(
    member_pairs: pa.Table,
    instruments: pa.Table,
) -> tuple[pa.Table, pa.Table]:
    """Split index member pairs by the provider instrument listing window."""
    if not member_pairs.num_rows:
        return member_pairs, member_pairs
    if not instruments.num_rows:
        return member_pairs.slice(0, 0), member_pairs.slice(0, 0)

    instrument_indexes = pc.index_in(
        member_pairs["code"],
        value_set=instruments["code"],
    )
    known_instrument = pc.is_valid(instrument_indexes)
    ipo_dates = pc.take(instruments["ipo_date"], instrument_indexes)
    out_dates = pc.take(instruments["out_date"], instrument_indexes)
    on_or_after_ipo = pc.greater_equal(member_pairs["trade_date"], ipo_dates)
    before_out = pc.or_kleene(
        pc.is_null(out_dates),
        pc.less(member_pairs["trade_date"], out_dates),
    )
    in_listing_window = pc.and_kleene(on_or_after_ipo, before_out)
    listed = pc.and_kleene(known_instrument, in_listing_window)
    excluded = pc.and_kleene(known_instrument, pc.invert(in_listing_window))
    return member_pairs.filter(listed), member_pairs.filter(excluded)


def _received_at_bounds(
    tables: tuple[pa.Table, ...],
) -> tuple[datetime | None, datetime | None]:
    bounds = [
        pc.min_max(table["source_received_at"]).as_py()
        for table in tables
        if table.num_rows
    ]
    if not bounds:
        return None, None
    return (
        min(bound["min"] for bound in bounds),
        max(bound["max"] for bound in bounds),
    )


def _duplicate_member_pair_count(table: pa.Table) -> int:
    if not table.num_rows:
        return 0
    grouped = table.group_by(["snapshot_date"]).aggregate(
        [("member_code", "count_distinct")]
    )
    distinct_pairs = sum(row["member_code_count_distinct"] for row in grouped.to_pylist())
    return table.num_rows - distinct_pairs


def _left_anti(left: pa.Table, right: pa.Table) -> pa.Table:
    if not left.num_rows:
        return left
    if not right.num_rows:
        return left
    return left.join(right, keys=["trade_date", "code"], join_type="left anti")


def _late_row_count(table: pa.Table, date_column: str) -> int:
    if not table.num_rows:
        return 0
    midnight = pc.cast(table[date_column], pa.timestamp("us", tz="UTC"))
    cutoff = pc.add(midnight, pa.scalar(DECISION_CUTOFF_UTC, type=pa.duration("us")))
    return int(pc.sum(pc.cast(pc.greater(table["available_at"], cutoff), pa.int64())).as_py())


def _trade_status_mismatches(
    member_pairs: pa.Table,
    universe: pa.Table,
    signal: pa.Table,
    execution: pa.Table,
) -> pa.Table:
    if not universe.num_rows or not signal.num_rows or not execution.num_rows:
        return pa.table({"trade_date": [], "code": []})
    left = universe.select(["snapshot_date", "code", "trade_status"]).rename_columns(
        ["trade_date", "code", "universe_trade_status"]
    )
    middle = signal.select(["trade_date", "code", "trade_status"]).rename_columns(
        ["trade_date", "code", "signal_trade_status"]
    )
    right = execution.select(["trade_date", "code", "trade_status"]).rename_columns(
        ["trade_date", "code", "daily_trade_status"]
    )
    relevant = member_pairs.join(
        left,
        keys=["trade_date", "code"],
        join_type="inner",
    )
    relevant = relevant.join(middle, keys=["trade_date", "code"], join_type="inner")
    joined = relevant.join(right, keys=["trade_date", "code"], join_type="inner")
    mismatches = joined.filter(
        pc.or_(
            pc.not_equal(
                joined["universe_trade_status"],
                joined["signal_trade_status"],
            ),
            pc.not_equal(
                joined["universe_trade_status"],
                joined["daily_trade_status"],
            ),
        )
    )
    return mismatches.select(["trade_date", "code"])


def _pair_samples(table: pa.Table) -> tuple[str, ...]:
    return tuple(
        f"{row['trade_date'].isoformat()}:{row['code']}"
        for row in table.slice(0, SAMPLE_LIMIT).to_pylist()
    )
