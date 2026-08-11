import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from trading_codex.data.parquet_store import ParquetDataStore

INDEX_CODES = ("sh.000300", "sh.000905")
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
    return parser


if __name__ == "__main__":
    main()
