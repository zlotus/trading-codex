import argparse
import json
import os
import sys
from pathlib import Path

from trading_codex.baostock_download.constants import (
    DEFAULT_DATA_ROOT,
    default_state_root,
)
from trading_codex.baostock_download.errors import (
    BaoStockDownloadError,
    ProviderBlacklisted,
    ProviderFailure,
    ProviderLockError,
)
from trading_codex.baostock_download.requests import read_jsonl

EXIT_BLOCKED = 2
EXIT_STORAGE = 3
EXIT_LOCKED = 4
EXIT_PROVIDER = 5
EXIT_BLACKLISTED = 6


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    data_root = args.data_root.resolve()
    try:
        if args.requests == "-":
            requests = read_jsonl(sys.stdin.buffer, source="<stdin>")
        else:
            request_path = Path(args.requests)
            with request_path.open("rb") as stream:
                requests = read_jsonl(stream, source=str(request_path))

        # Provider and downloader code load only after the local request stream
        # has parsed successfully.
        from trading_codex.baostock_download.downloader import download

        payload = download(
            data_root=data_root,
            state_root=default_state_root(),
            requests=requests,
            progress=_print_progress,
        )
    except ProviderBlacklisted as exc:
        _error(exc, code=EXIT_BLACKLISTED)
    except ProviderLockError as exc:
        _error(exc, code=EXIT_LOCKED)
    except ProviderFailure as exc:
        _error(exc, code=EXIT_PROVIDER)
    except OSError as exc:
        _error(exc, code=EXIT_STORAGE)
    except (BaoStockDownloadError, RuntimeError, ValueError) as exc:
        _error(exc, code=EXIT_BLOCKED)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-codex-baostock",
        description=(
            "Serial BaoStock raw downloader. Read exact requests from JSONL, skip "
            "existing query-addressed files, and stop after the first error."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("BAOSTOCK_DATA_ROOT", DEFAULT_DATA_ROOT)),
        help="directory that receives raw BaoStock envelopes",
    )
    parser.add_argument(
        "--requests",
        default="-",
        metavar="JSONL",
        help="JSONL request file, or - to read standard input (default)",
    )
    return parser


def _print_progress(event: dict[str, object]) -> None:
    print(
        f"[{event['index']}/{event['total']}] {event['operation']} "
        f"rows={event['rows']} -> {event['raw_file']}",
        file=sys.stderr,
        flush=True,
    )


def _error(error: Exception, *, code: int) -> None:
    print(
        json.dumps(
            {
                "status": "blocked",
                "error_type": type(error).__name__,
                "reason": str(error),
                "exit_code": code,
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=sys.stderr,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
