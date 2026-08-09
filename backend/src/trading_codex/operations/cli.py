import argparse
import json
import sqlite3
import sys
from pathlib import Path

from trading_codex.ledger.models import LedgerError
from trading_codex.ledger.store import SQLiteLedger
from trading_codex.operations.backup import (
    BackupError,
    create_backup,
    replay_backup,
    verify_backup,
)
from trading_codex.operations.review import (
    MINIMUM_FORWARD_TRADING_DAYS,
    ForwardReviewBuilder,
    ObservationWindowError,
    write_forward_review,
)


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            manifest = create_backup(args.ledger_path, args.destination)
            _print(
                {
                    "status": "created",
                    "manifest_path": str(manifest.manifest_path),
                    **manifest.as_dict(),
                }
            )
            return
        if args.command == "verify-backup":
            _print(verify_backup(args.manifest).as_dict())
            return
        if args.command == "replay-backup":
            _print(replay_backup(args.manifest).as_dict())
            return
        if args.command == "review":
            builder = ForwardReviewBuilder(
                minimum_trading_days=args.minimum_trading_days
            )
            ledger = SQLiteLedger(args.ledger_path)
            report = builder.build(ledger.list_forward_observations())
            path = write_forward_review(report, args.output_directory)
            _print({"status": "created", "report": str(path), **report.as_dict()})
            return
    except (
        BackupError,
        LedgerError,
        ObservationWindowError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as error:
        _print({"status": "blocked", "reason": str(error)}, stream=sys.stderr)
        raise SystemExit(2) from error
    parser.error(f"unknown command: {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-codex-ops")
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create a consistent immutable ledger backup")
    backup.add_argument("--ledger-path", type=Path, default=Path("data/trading-codex.db"))
    backup.add_argument("--destination", type=Path, required=True)

    verify = commands.add_parser("verify-backup", help="verify a backup manifest and database")
    verify.add_argument("manifest", type=Path)

    replay = commands.add_parser("replay-backup", help="replay portfolio state from a backup")
    replay.add_argument("manifest", type=Path)

    review = commands.add_parser("review", help="build a forward attribution report")
    review.add_argument("--ledger-path", type=Path, default=Path("data/trading-codex.db"))
    review.add_argument("--output-directory", type=Path, default=Path("artifacts/forward-review"))
    review.add_argument(
        "--minimum-trading-days",
        type=int,
        default=MINIMUM_FORWARD_TRADING_DAYS,
        help="require at least 60 trading days; larger values make the gate stricter",
    )
    return parser


def _print(payload: dict[str, object], *, stream: object = sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


if __name__ == "__main__":
    main()
