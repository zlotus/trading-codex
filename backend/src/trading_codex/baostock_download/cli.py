import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_codex.baostock_download.constants import (
    DEFAULT_DATA_ROOT,
    DEFAULT_MAX_ITEMS,
    MAX_SESSION_ITEMS,
    default_state_root,
    provider_rule_snapshot,
)
from trading_codex.baostock_download.errors import (
    BaoStockDownloadError,
    BudgetExceeded,
    ManifestError,
    OfflineSyncError,
    ProviderBlacklisted,
    ProviderFailure,
    ProviderLockError,
    StateError,
    StoragePreflightError,
)
from trading_codex.baostock_download.manifest import (
    create_manifest,
    freeze_manifest,
    load_manifest,
    require_frozen,
    strict_json_loads,
    write_draft,
)
from trading_codex.baostock_download.offline import (
    data_root_lock,
    import_raw_cache,
    manifest_status,
    sync_manifest,
    verify_manifest,
)
from trading_codex.baostock_download.state import GlobalProviderLock, StateStore
from trading_codex.baostock_download.storage import StorageGuard
from trading_codex.data.models import MarketDataError

EXIT_BLOCKED = 2
EXIT_STORAGE = 3
EXIT_LOCKED = 4
EXIT_PROVIDER = 5
EXIT_BLACKLISTED = 6


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    data_root = args.data_root.resolve()
    state = StateStore(default_state_root())
    storage = StorageGuard(data_root=data_root)
    try:
        payload = _run(args, data_root=data_root, state=state, storage=storage)
    except ProviderBlacklisted as exc:
        _error(exc, code=EXIT_BLACKLISTED)
    except ProviderLockError as exc:
        _error(exc, code=EXIT_LOCKED)
    except StoragePreflightError as exc:
        _error(exc, code=EXIT_STORAGE)
    except ProviderFailure as exc:
        _error(exc, code=EXIT_PROVIDER)
    except (ManifestError, BudgetExceeded, StateError, ValueError) as exc:
        _error(exc, code=EXIT_BLOCKED)
    except MarketDataError as exc:
        _error(exc, code=EXIT_BLOCKED)
    except OSError as exc:
        _error(
            StoragePreflightError(f"filesystem operation failed: {exc}"),
            code=EXIT_STORAGE,
        )
    except BaoStockDownloadError as exc:
        _error(exc, code=EXIT_BLOCKED)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_value))


def _run(
    args: argparse.Namespace,
    *,
    data_root: Path,
    state: StateStore,
    storage: StorageGuard,
) -> dict[str, object]:
    if args.command == "doctor":
        storage_report = storage.preflight(initialize=args.initialize)
        if args.initialize:
            state.initialize()
        state_report = state.inspect()
        state.mirror_to(data_root / "state" / "request-audit.sqlite")
        return {
            "status": "passed",
            "network_access": False,
            "storage": storage_report.as_dict(),
            "global_state": state_report,
            "provider_rules": provider_rule_snapshot(),
        }

    if args.command == "plan":
        if args.plan_command == "create":
            spec = _read_json(args.spec)
            manifest = create_manifest(spec, created_at=datetime.now(UTC))
            storage.preflight(
                initialize=False,
                estimated_peak_bytes=manifest.estimated_peak_bytes,
            )
            state.inspect()
            path = write_draft(data_root, manifest)
            return {
                "status": "draft",
                "network_access": False,
                "manifest": str(path),
                "manifest_id": manifest.manifest_id,
                "manifest_sha256": manifest.manifest_sha256,
                "items": len(manifest.items),
            }
        if args.plan_command == "show":
            manifest = load_manifest(args.manifest)
            return {"network_access": False, **manifest.as_dict()}
        if args.plan_command == "freeze":
            storage.preflight(initialize=False)
            state.inspect()
            manifest, path = freeze_manifest(data_root, args.manifest)
            return {
                "status": "frozen",
                "network_access": False,
                "manifest": str(path),
                "manifest_id": manifest.manifest_id,
                "manifest_sha256": manifest.manifest_sha256,
                "items": len(manifest.items),
            }

    if args.command == "status":
        manifest = require_frozen(data_root, args.manifest)
        storage.preflight(initialize=False)
        state.inspect()
        with data_root_lock(data_root):
            report = manifest_status(data_root=data_root, manifest=manifest, state=state)
        return {
            "network_access": False,
            **report,
        }

    if args.command == "fetch":
        manifest = require_frozen(data_root, args.manifest)
        # This import is the only CLI branch that can load the provider adapter.
        from trading_codex.baostock_download.online import fetch_manifest

        return {
            "network_access": True,
            **fetch_manifest(
                data_root=data_root,
                manifest=manifest,
                confirmed_sha256=args.confirm_manifest_sha256,
                max_items=args.max_items,
                state=state,
                storage=storage,
            ),
        }

    if args.command == "sync":
        manifest = require_frozen(data_root, args.manifest)
        storage.preflight(
            initialize=False,
            estimated_peak_bytes=manifest.estimated_peak_bytes,
        )
        state.inspect()
        report = sync_manifest(data_root=data_root, manifest=manifest, state=state)
        state.mirror_to(data_root / "state" / "request-audit.sqlite")
        return {
            "network_access": False,
            **report,
        }

    if args.command == "verify":
        manifest = require_frozen(data_root, args.manifest)
        storage.preflight(initialize=False)
        state.inspect()
        report = verify_manifest(
            data_root=data_root,
            manifest=manifest,
            state=state,
            as_of=args.as_of,
        )
        if report["status"] != "passed":
            raise OfflineSyncError(
                f"manifest verification failed; inspect {report['report']}"
            )
        state.mirror_to(data_root / "state" / "request-audit.sqlite")
        return {
            "network_access": False,
            **report,
        }

    if args.command == "import-raw":
        storage.preflight(initialize=False)
        state.inspect()
        return {
            "network_access": False,
            **import_raw_cache(
                source_root=args.source_root,
                data_root=data_root,
                source_provider_client_version=args.source_provider_client_version,
            ),
        }

    if args.command == "recover":
        state.inspect()
        storage.preflight(initialize=False)
        with data_root_lock(data_root), GlobalProviderLock(state.root):
            state.inspect()
            if args.recover_command == "session":
                state.abandon_session(
                    session_id=args.session_id,
                    operator=args.operator,
                    reason=args.reason,
                )
                result = {
                    "status": "recorded",
                    "recovery": "session_abandoned",
                    "session_id": args.session_id,
                }
            elif args.recover_command == "blacklist":
                recovery_id = state.recover_blacklist(
                    incident_id=args.incident_id,
                    operator=args.operator,
                    administrator_confirmation=args.administrator_confirmation,
                    reason=args.reason,
                )
                result = {
                    "status": "recorded",
                    "recovery": "provider_blacklist_cleared",
                    "incident_id": args.incident_id,
                    "recovery_id": recovery_id,
                }
            else:
                raise ManifestError("unknown recovery command")
            state.mirror_to(data_root / "state" / "request-audit.sqlite")
        return {"network_access": False, **result}

    raise ManifestError(f"unknown command: {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-codex-baostock",
        description=(
            "Manifest-driven serial BaoStock downloader. Only the fetch command can "
            "load the provider network adapter."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("BAOSTOCK_DATA_ROOT", DEFAULT_DATA_ROOT)),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="run zero-network storage and state checks")
    doctor.add_argument(
        "--initialize",
        action="store_true",
        help="create the data layout and global state if absent",
    )
    _json_flag(doctor)

    plan = commands.add_parser("plan", help="create, inspect, or freeze a zero-network manifest")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    create = plan_commands.add_parser("create", help="create a deterministic draft from a spec")
    create.add_argument("--spec", type=Path, required=True)
    _json_flag(create)
    show = plan_commands.add_parser("show", help="verify and display a manifest")
    show.add_argument("--manifest", type=Path, required=True)
    _json_flag(show)
    freeze = plan_commands.add_parser("freeze", help="immutably freeze a draft manifest")
    freeze.add_argument("--manifest", type=Path, required=True)
    _json_flag(freeze)

    status = commands.add_parser("status", help="show raw and attempt status without network")
    status.add_argument("--manifest", type=Path, required=True)
    _json_flag(status)

    fetch = commands.add_parser("fetch", help="perform the only permitted live BaoStock fetch")
    fetch.add_argument("--manifest", type=Path, required=True)
    fetch.add_argument("--confirm-manifest-sha256", required=True)
    fetch.add_argument(
        "--max-items",
        type=int,
        choices=range(1, MAX_SESSION_ITEMS + 1),
        default=DEFAULT_MAX_ITEMS,
    )
    _json_flag(fetch)

    sync = commands.add_parser("sync", help="publish normalized Parquet from local raw only")
    sync.add_argument("--manifest", type=Path, required=True)
    _json_flag(sync)

    verify = commands.add_parser("verify", help="verify raw, normalized, and manifest completion")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--as-of", type=_datetime, required=True)
    _json_flag(verify)

    import_raw = commands.add_parser("import-raw", help="import an existing raw cache offline")
    import_raw.add_argument("--source-root", type=Path, required=True)
    import_raw.add_argument(
        "--source-provider-client-version",
        required=True,
        choices=("00.9.30",),
        help="explicitly attest the client version that created the source cache",
    )
    _json_flag(import_raw)

    recover = commands.add_parser("recover", help="append a reviewed manual recovery event")
    recover_commands = recover.add_subparsers(dest="recover_command", required=True)
    session = recover_commands.add_parser("session", help="mark a crashed session abandoned")
    session.add_argument("--session-id", required=True)
    session.add_argument("--operator", required=True)
    session.add_argument("--reason", required=True)
    _json_flag(session)
    blacklist = recover_commands.add_parser(
        "blacklist", help="record administrator-confirmed blacklist removal"
    )
    blacklist.add_argument("--incident-id", required=True)
    blacklist.add_argument("--operator", required=True)
    blacklist.add_argument("--administrator-confirmation", required=True)
    blacklist.add_argument("--reason", required=True)
    _json_flag(blacklist)
    return parser


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit the stable JSON result schema")


def _read_json(path: Path) -> Any:
    try:
        return strict_json_loads(path.read_bytes())
    except FileNotFoundError as exc:
        raise ManifestError(f"JSON input does not exist: {path}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"JSON input is invalid: {path}") from exc


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed


def _json_value(value: Any) -> str:
    if isinstance(value, (Path, datetime)):
        return str(value) if isinstance(value, Path) else value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


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
