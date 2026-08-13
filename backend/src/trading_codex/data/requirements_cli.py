import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.compute as pc

from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.point_in_time import (
    DEFAULT_BENCHMARK_CODE,
    INDEX_CODES,
    INDEX_MEMBER_COUNTS,
    expected_index_member_count,
)

DEFAULT_START_DATE = date(2011, 1, 1)
DEFAULT_END_DATE = date(2026, 8, 10)


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "index-memberships":
        requests = (
            {
                "operation": "hs300_stocks",
                "query": {"date": args.date.isoformat()},
            },
            {
                "operation": "zz500_stocks",
                "query": {"date": args.date.isoformat()},
            },
        )
    elif args.command == "base-daily":
        requests = _base_daily_requests(
            data_root=args.data_root,
            snapshot_date=args.snapshot_date,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    elif args.command == "point-in-time":
        requests = _point_in_time_requests(
            data_root=args.data_root,
            start_date=args.start_date,
            end_date=args.end_date,
            benchmark_code=args.benchmark_code,
        )
    elif args.command == "member-daily":
        requests = _member_daily_requests(
            data_root=args.data_root,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    else:
        raise RuntimeError(f"unknown requirements command: {args.command}")

    for request in requests:
        print(json.dumps(request, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    print(f"generated {len(requests)} BaoStock requests", file=sys.stderr)


def _base_daily_requests(
    *,
    data_root: Path,
    snapshot_date: date | None,
    start_date: date,
    end_date: date,
) -> tuple[dict[str, Any], ...]:
    if end_date < start_date:
        raise ValueError("end-date must not precede start-date")
    rows = ParquetDataStore(data_root.resolve() / "normalized").read(
        "index_memberships"
    ).to_pylist()
    available_dates = sorted(
        {
            row["snapshot_date"]
            for row in rows
            if row["index_code"] in INDEX_CODES
        }
    )
    if not available_dates:
        raise ValueError(
            "normalized index_memberships is empty; generate and download "
            "index-memberships requests first"
        )
    selected_date = snapshot_date or available_dates[-1]
    by_index = {
        index_code: {
            row["member_code"]
            for row in rows
            if row["snapshot_date"] == selected_date
            and row["index_code"] == index_code
        }
        for index_code in INDEX_CODES
    }
    missing = [index_code for index_code, codes in by_index.items() if not codes]
    if missing:
        raise ValueError(
            f"snapshot {selected_date} is missing index members for {', '.join(missing)}"
        )
    codes = sorted(set().union(*by_index.values()))
    requests: list[dict[str, Any]] = [
        {"operation": "instruments", "query": {"code": ""}},
        {
            "operation": "trade_calendar",
            "query": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        },
    ]
    for code in codes:
        for adjustment_flag in ("2", "3"):
            requests.append(
                {
                    "operation": "daily_bars",
                    "query": {
                        "code": code,
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                        "frequency": "d",
                        "adjustflag": adjustment_flag,
                    },
                }
            )
    return tuple(requests)


def _point_in_time_requests(
    *,
    data_root: Path,
    start_date: date,
    end_date: date,
    benchmark_code: str,
) -> tuple[dict[str, Any], ...]:
    trading_days = _trading_days(
        data_root=data_root,
        start_date=start_date,
        end_date=end_date,
    )
    _validate_code(benchmark_code, field="benchmark-code")
    requests: list[dict[str, Any]] = []
    for day in trading_days:
        requests.extend(
            (
                {
                    "operation": "hs300_stocks",
                    "query": {"date": day.isoformat()},
                },
                {
                    "operation": "zz500_stocks",
                    "query": {"date": day.isoformat()},
                },
                {
                    "operation": "historical_universe",
                    "query": {"day": day.isoformat()},
                },
            )
        )
    requests.append(
        {
            "operation": "daily_bars",
            "query": {
                "code": benchmark_code,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "frequency": "d",
                "adjustflag": "3",
            },
        }
    )
    return tuple(requests)


def _member_daily_requests(
    *,
    data_root: Path,
    start_date: date,
    end_date: date,
    expected_index_counts: dict[str, int] | None = None,
) -> tuple[dict[str, Any], ...]:
    trading_days = _trading_days(
        data_root=data_root,
        start_date=start_date,
        end_date=end_date,
    )
    store = ParquetDataStore(data_root.resolve() / "normalized")
    memberships = store.scan(
        "index_memberships",
        as_of=datetime.now(UTC),
        columns=("snapshot_date", "index_code", "member_code"),
        contained_in={"index_code": INDEX_CODES},
        ranges={"snapshot_date": (start_date, end_date)},
    )
    expected_counts = expected_index_counts or INDEX_MEMBER_COUNTS
    grouped = memberships.group_by(["snapshot_date", "index_code"]).aggregate(
        [("member_code", "count_distinct")]
    )
    counts = {
        (row["snapshot_date"], row["index_code"]): row["member_code_count_distinct"]
        for row in grouped.to_pylist()
    }
    incomplete = [
        f"{day.isoformat()}:{index_code}:{counts.get((day, index_code), 0)}"
        for day in trading_days
        for index_code in expected_counts
        if counts.get((day, index_code), 0)
        != expected_index_member_count(
            day,
            index_code,
            base_counts=expected_index_counts,
        )
    ]
    if incomplete:
        raise ValueError(
            "point-in-time index memberships are incomplete; first failures: "
            + ", ".join(incomplete[:5])
        )
    codes = sorted(pc.unique(memberships["member_code"]).to_pylist())
    requests = []
    for code in codes:
        for adjustment_flag in ("2", "3"):
            requests.append(
                {
                    "operation": "daily_bars",
                    "query": {
                        "code": code,
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                        "frequency": "d",
                        "adjustflag": adjustment_flag,
                    },
                }
            )
    return tuple(requests)


def _trading_days(
    *,
    data_root: Path,
    start_date: date,
    end_date: date,
) -> tuple[date, ...]:
    if end_date < start_date:
        raise ValueError("end-date must not precede start-date")
    requested_dates = tuple(
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    )
    rows = ParquetDataStore(data_root.resolve() / "normalized").scan(
        "trade_calendar",
        as_of=datetime.now(UTC),
        columns=("calendar_date", "is_trading_day"),
        ranges={"calendar_date": (start_date, end_date)},
    ).to_pylist()
    by_date = {row["calendar_date"]: row["is_trading_day"] for row in rows}
    missing = [day for day in requested_dates if day not in by_date]
    if missing:
        raise ValueError(
            "normalized trade calendar is incomplete; first missing dates: "
            + ", ".join(day.isoformat() for day in missing[:5])
        )
    trading_days = tuple(day for day in requested_dates if by_date[day])
    if not trading_days:
        raise ValueError("requested interval has no trading days")
    return trading_days


def _validate_code(value: str, *, field: str) -> None:
    if len(value) != 9 or value[2] != "." or value[:2] not in {"sh", "sz", "bj"}:
        raise ValueError(f"{field} must be a BaoStock code")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-codex-requirements",
        description="Emit BaoStock exact requests as JSONL without network access.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    memberships = commands.add_parser(
        "index-memberships", help="request one HS300 and ZZ500 membership snapshot"
    )
    memberships.add_argument("--date", type=date.fromisoformat, required=True)

    base = commands.add_parser(
        "base-daily",
        help="emit dual-price daily requests for the HS300 and ZZ500 union",
    )
    base.add_argument("--data-root", type=Path, required=True)
    base.add_argument("--snapshot-date", type=date.fromisoformat)
    base.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START_DATE)
    base.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END_DATE)

    point_in_time = commands.add_parser(
        "point-in-time",
        help="emit daily index membership, historical universe, and benchmark requests",
    )
    point_in_time.add_argument("--data-root", type=Path, required=True)
    point_in_time.add_argument(
        "--start-date", type=date.fromisoformat, default=DEFAULT_START_DATE
    )
    point_in_time.add_argument(
        "--end-date", type=date.fromisoformat, default=DEFAULT_END_DATE
    )
    point_in_time.add_argument("--benchmark-code", default=DEFAULT_BENCHMARK_CODE)

    member_daily = commands.add_parser(
        "member-daily",
        help="emit dual-price daily requests for every point-in-time index member",
    )
    member_daily.add_argument("--data-root", type=Path, required=True)
    member_daily.add_argument(
        "--start-date", type=date.fromisoformat, default=DEFAULT_START_DATE
    )
    member_daily.add_argument(
        "--end-date", type=date.fromisoformat, default=DEFAULT_END_DATE
    )
    return parser


if __name__ == "__main__":
    main()
