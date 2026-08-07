import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_codex.data.cached_client import CachedBaoStockClient
from trading_codex.data.models import CacheMissError, RequestBudgetExceeded
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.quality import (
    assess_opening_0935_coverage,
    inspect_data_quality,
    write_report,
)
from trading_codex.data.raw_store import ImmutableRawStore
from trading_codex.data.sync import BaoStockSyncService, IngestionPipeline


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    data_root = args.data_root.resolve()
    artifacts_root = args.artifacts_root.resolve()
    normalized_store = ParquetDataStore(data_root / "normalized")

    if args.command == "sync":
        codes = _codes(args.codes)
        raw_store = ImmutableRawStore(data_root / "raw")
        pipeline = IngestionPipeline(raw_store, normalized_store)
        client = CachedBaoStockClient(
            raw_store,
            allow_network=args.fetch_missing,
            max_upstream_requests=args.max_upstream_requests,
            minimum_request_interval=args.minimum_request_interval,
        )
        try:
            report = BaoStockSyncService(client, pipeline).sync(
                start_date=args.start_date,
                end_date=args.end_date,
                codes=codes,
                include_five_minute_bars=args.with_five_minute,
            )
        except (CacheMissError, RequestBudgetExceeded) as exc:
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": str(exc),
                        "cache_hits": client.cache_hits,
                        "cache_misses": client.cache_misses,
                        "upstream_requests": client.upstream_requests,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
        quality = inspect_data_quality(normalized_store, as_of=datetime.now(UTC))
        quality_path = write_report(quality, artifacts_root / "data-quality")
        payload = {
            **report.as_dict(),
            "quality_status": quality.status,
            "quality_report": str(quality_path),
            "cache_hits": client.cache_hits,
            "cache_misses": client.cache_misses,
            "upstream_requests": client.upstream_requests,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if quality.status != "passed":
            raise SystemExit(2)
        return

    if args.command == "quality":
        report = inspect_data_quality(normalized_store, as_of=args.as_of)
        path = write_report(report, artifacts_root / "data-quality")
        print(
            json.dumps(
                {**report.as_dict(), "report": str(path)},
                default=_json_value,
                ensure_ascii=False,
                indent=2,
            )
        )
        if report.status != "passed":
            raise SystemExit(2)
        return

    if args.command == "assess-0935":
        report = assess_opening_0935_coverage(
            normalized_store,
            codes=_codes(args.codes),
            start_date=args.start_date,
            end_date=args.end_date,
            as_of=args.as_of,
        )
        path = write_report(report, artifacts_root / "data-quality")
        print(
            json.dumps(
                {**report.as_dict(), "report": str(path)},
                default=_json_value,
                ensure_ascii=False,
                indent=2,
            )
        )
        if report.status != "passed":
            raise SystemExit(2)
        return

    parser.error(f"unknown command: {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-codex-data")
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts"))
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="sync a bounded BaoStock date range")
    _date_range(sync)
    sync.add_argument("--codes", required=True, help="comma-separated BaoStock codes")
    sync.add_argument("--with-five-minute", action="store_true")
    sync.add_argument(
        "--fetch-missing",
        action="store_true",
        help="allow upstream access for cache misses; disabled by default",
    )
    sync.add_argument(
        "--max-upstream-requests",
        type=int,
        choices=(0, 1),
        default=1,
        help="hard-capped at one upstream data request per process",
    )
    sync.add_argument("--minimum-request-interval", type=float, default=3.0)

    quality = commands.add_parser("quality", help="validate normalized datasets")
    quality.add_argument("--as-of", type=_datetime, default=datetime.now(UTC))

    coverage = commands.add_parser("assess-0935", help="measure 09:35 bar coverage")
    _date_range(coverage)
    coverage.add_argument("--codes", required=True, help="comma-separated BaoStock codes")
    coverage.add_argument("--as-of", type=_datetime, default=datetime.now(UTC))
    return parser


def _date_range(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed


def _codes(value: str) -> list[str]:
    codes = [code.strip().lower() for code in value.split(",") if code.strip()]
    if not codes:
        raise argparse.ArgumentTypeError("at least one code is required")
    invalid = [code for code in codes if len(code) != 9 or code[2] != "."]
    if invalid:
        raise argparse.ArgumentTypeError(f"invalid BaoStock codes: {', '.join(invalid)}")
    return codes


def _json_value(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


if __name__ == "__main__":
    main()
