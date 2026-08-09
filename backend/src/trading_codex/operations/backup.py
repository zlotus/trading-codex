from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_codex.ledger.models import PortfolioTrack, as_utc
from trading_codex.ledger.store import SQLiteLedger

BACKUP_MANIFEST_VERSION = "trading-codex-ledger-backup-v1"
REQUIRED_LEDGER_TABLES = frozenset(
    {"decision_runs", "fills", "cash_movements", "job_runs", "job_attempt_events"}
)


class BackupError(RuntimeError):
    """A backup is missing, inconsistent, or cannot be replayed safely."""


@dataclass(frozen=True)
class DatabaseFingerprint:
    schema_version: int
    integrity: str
    foreign_key_errors: int
    missing_append_only_triggers: tuple[str, ...]
    table_counts: dict[str, int]
    content_sha256: str


@dataclass(frozen=True)
class BackupManifest:
    version: str
    created_at: datetime
    database_file: str
    database_sha256: str
    database_bytes: int
    schema_version: int
    content_sha256: str
    table_counts: dict[str, int]
    manifest_path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "created_at": _datetime_text(self.created_at),
            "database_file": self.database_file,
            "database_sha256": self.database_sha256,
            "database_bytes": self.database_bytes,
            "schema_version": self.schema_version,
            "content_sha256": self.content_sha256,
            "table_counts": self.table_counts,
        }


@dataclass(frozen=True)
class BackupVerification:
    manifest: BackupManifest
    verified_at: datetime
    fingerprint: DatabaseFingerprint

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "verified",
            "verified_at": _datetime_text(self.verified_at),
            "manifest": self.manifest.as_dict(),
            "integrity": self.fingerprint.integrity,
            "foreign_key_errors": self.fingerprint.foreign_key_errors,
            "missing_append_only_triggers": list(
                self.fingerprint.missing_append_only_triggers
            ),
        }


@dataclass(frozen=True)
class ReplayAudit:
    verified_at: datetime
    replayed_as_of: datetime
    source_schema_version: int
    replay_schema_version: int
    source_content_sha256: str
    replay_content_sha256: str
    table_counts: dict[str, int]
    track_cash: dict[str, str]
    track_equity: dict[str, str | None]
    track_positions: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = "passed"
        payload["verified_at"] = _datetime_text(self.verified_at)
        payload["replayed_as_of"] = _datetime_text(self.replayed_as_of)
        return payload


def create_backup(
    ledger_path: Path,
    destination: Path,
    *,
    created_at: datetime | None = None,
) -> BackupManifest:
    source = ledger_path.resolve()
    if not source.is_file():
        raise BackupError(f"ledger database does not exist: {source}")
    created = as_utc(created_at or datetime.now(UTC), field="created_at")
    directory = destination.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".ledger-backup-", suffix=".db.tmp", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
        temporary_path.chmod(0o600)
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=5)
        target_connection = sqlite3.connect(temporary_path)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()

        fingerprint = fingerprint_database(temporary_path)
        _require_valid_fingerprint(fingerprint)
        database_sha256 = _file_sha256(temporary_path)
        timestamp = created.strftime("%Y%m%dT%H%M%S%fZ")
        database_name = f"ledger-{timestamp}-{database_sha256[:16]}.db"
        database_path = directory / database_name
        _publish_file(temporary_path, database_path, expected_sha256=database_sha256)
        temporary_path = None

        manifest_path = database_path.with_suffix(".manifest.json")
        manifest = BackupManifest(
            version=BACKUP_MANIFEST_VERSION,
            created_at=created,
            database_file=database_name,
            database_sha256=database_sha256,
            database_bytes=database_path.stat().st_size,
            schema_version=fingerprint.schema_version,
            content_sha256=fingerprint.content_sha256,
            table_counts=fingerprint.table_counts,
            manifest_path=manifest_path,
        )
        _write_immutable_json(manifest_path, manifest.as_dict())
        return manifest
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def verify_backup(
    manifest_path: Path, *, verified_at: datetime | None = None
) -> BackupVerification:
    manifest = _read_manifest(manifest_path.resolve())
    database_path = manifest.manifest_path.parent / manifest.database_file
    if not database_path.is_file():
        raise BackupError("backup database referenced by manifest does not exist")
    if database_path.stat().st_size != manifest.database_bytes:
        raise BackupError("backup database size differs from manifest")
    if _file_sha256(database_path) != manifest.database_sha256:
        raise BackupError("backup database SHA-256 differs from manifest")
    fingerprint = fingerprint_database(database_path)
    _require_valid_fingerprint(fingerprint)
    if fingerprint.schema_version != manifest.schema_version:
        raise BackupError("backup schema version differs from manifest")
    if fingerprint.content_sha256 != manifest.content_sha256:
        raise BackupError("backup logical content differs from manifest")
    if fingerprint.table_counts != manifest.table_counts:
        raise BackupError("backup table counts differ from manifest")
    return BackupVerification(
        manifest=manifest,
        verified_at=as_utc(verified_at or datetime.now(UTC), field="verified_at"),
        fingerprint=fingerprint,
    )


def replay_backup(
    manifest_path: Path, *, verified_at: datetime | None = None
) -> ReplayAudit:
    verification = verify_backup(manifest_path, verified_at=verified_at)
    source = verification.manifest.manifest_path.parent / verification.manifest.database_file
    with tempfile.TemporaryDirectory(prefix="trading-codex-replay-") as temporary_directory:
        replay_path = Path(temporary_directory) / "replay.db"
        shutil.copy2(source, replay_path)
        ledger = SQLiteLedger(replay_path)
        replayed_as_of = _latest_ledger_time(replay_path)
        dashboard = ledger.dashboard(as_of=replayed_as_of)
        fingerprint = fingerprint_database(replay_path)
        _require_valid_fingerprint(fingerprint)
        tracks = {track.track: track for track in dashboard.tracks}
        return ReplayAudit(
            verified_at=verification.verified_at,
            replayed_as_of=replayed_as_of,
            source_schema_version=verification.manifest.schema_version,
            replay_schema_version=fingerprint.schema_version,
            source_content_sha256=verification.manifest.content_sha256,
            replay_content_sha256=fingerprint.content_sha256,
            table_counts=fingerprint.table_counts,
            track_cash={track.value: str(tracks[track].cash) for track in PortfolioTrack},
            track_equity={
                track.value: (
                    str(tracks[track].equity) if tracks[track].equity is not None else None
                )
                for track in PortfolioTrack
            },
            track_positions={
                track.value: len(tracks[track].positions) for track in PortfolioTrack
            },
        )


def fingerprint_database(path: Path) -> DatabaseFingerprint:
    resolved = path.resolve()
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema_rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()
        tables = tuple(row["name"] for row in schema_rows if row["type"] == "table")
        triggers = {row["name"] for row in schema_rows if row["type"] == "trigger"}
        missing_triggers = tuple(
            trigger
            for table in SQLiteLedger._APPEND_ONLY_TABLES
            if table in tables
            for trigger in (f"{table}_reject_update", f"{table}_reject_delete")
            if trigger not in triggers
        )
        counts: dict[str, int] = {}
        hasher = hashlib.sha256()
        for row in schema_rows:
            _hash_json(hasher, list(row))
        for table in sorted(tables):
            quoted = _quote_identifier(table)
            columns = tuple(
                row["name"] for row in connection.execute(f"PRAGMA table_info({quoted})")
            )
            rows = connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall()
            counts[table] = len(rows)
            _hash_json(hasher, {"table": table, "columns": columns})
            for row in rows:
                _hash_json(hasher, list(row))
        return DatabaseFingerprint(
            schema_version=schema_version,
            integrity=integrity,
            foreign_key_errors=foreign_key_errors,
            missing_append_only_triggers=missing_triggers,
            table_counts=counts,
            content_sha256=hasher.hexdigest(),
        )
    finally:
        connection.close()


def _latest_ledger_time(path: Path) -> datetime:
    candidates = (
        ("decision_runs", "recorded_at"),
        ("fills", "occurred_at"),
        ("cash_movements", "occurred_at"),
        ("signal_dispositions", "occurred_at"),
        ("job_attempt_events", "occurred_at"),
        ("ai_runs", "recorded_at"),
        ("provider_health_checks", "checked_at"),
        ("alert_events", "occurred_at"),
        ("forward_observations", "observed_at"),
    )
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        values = [
            row[0]
            for table, column in candidates
            if table in tables
            if (row := connection.execute(f"SELECT MAX({_quote_identifier(column)}) FROM "
                                           f"{_quote_identifier(table)}").fetchone())[0]
            is not None
        ]
    finally:
        connection.close()
    if not values:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return max(_parse_datetime(value) for value in values)


def _read_manifest(path: Path) -> BackupManifest:
    if not path.is_file():
        raise BackupError("backup manifest does not exist")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError("backup manifest is not valid JSON") from error
    required = {
        "version",
        "created_at",
        "database_file",
        "database_sha256",
        "database_bytes",
        "schema_version",
        "content_sha256",
        "table_counts",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise BackupError("backup manifest fields do not match the v1 contract")
    if payload["version"] != BACKUP_MANIFEST_VERSION:
        raise BackupError("unsupported backup manifest version")
    database_file = payload["database_file"]
    if not isinstance(database_file, str) or Path(database_file).name != database_file:
        raise BackupError("backup manifest database_file must be a sibling file name")
    for field_name in ("database_sha256", "content_sha256"):
        if not _is_sha256(payload[field_name]):
            raise BackupError(f"backup manifest {field_name} is invalid")
    if not isinstance(payload["table_counts"], dict) or not all(
        isinstance(key, str) and isinstance(value, int) and value >= 0
        for key, value in payload["table_counts"].items()
    ):
        raise BackupError("backup manifest table_counts are invalid")
    try:
        created_at = _parse_datetime(payload["created_at"])
        database_bytes = int(payload["database_bytes"])
        schema_version = int(payload["schema_version"])
    except (TypeError, ValueError) as error:
        raise BackupError("backup manifest scalar fields are invalid") from error
    if database_bytes < 0 or schema_version < 0:
        raise BackupError("backup manifest sizes and versions must be non-negative")
    return BackupManifest(
        version=payload["version"],
        created_at=created_at,
        database_file=database_file,
        database_sha256=payload["database_sha256"],
        database_bytes=database_bytes,
        schema_version=schema_version,
        content_sha256=payload["content_sha256"],
        table_counts=payload["table_counts"],
        manifest_path=path,
    )


def _require_valid_fingerprint(fingerprint: DatabaseFingerprint) -> None:
    if fingerprint.integrity != "ok":
        raise BackupError(f"SQLite quick_check failed: {fingerprint.integrity}")
    if fingerprint.foreign_key_errors:
        raise BackupError("SQLite foreign-key check failed")
    if fingerprint.missing_append_only_triggers:
        missing = ", ".join(fingerprint.missing_append_only_triggers)
        raise BackupError(f"append-only triggers are missing: {missing}")
    missing_tables = sorted(REQUIRED_LEDGER_TABLES - fingerprint.table_counts.keys())
    if missing_tables:
        raise BackupError(f"required ledger tables are missing: {', '.join(missing_tables)}")


def _publish_file(temporary: Path, destination: Path, *, expected_sha256: str) -> None:
    try:
        os.link(temporary, destination)
    except FileExistsError:
        if _file_sha256(destination) != expected_sha256:
            raise BackupError(f"refusing to overwrite existing backup: {destination}") from None
    temporary.unlink()


def _write_immutable_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise BackupError(f"refusing to overwrite existing manifest: {path}") from None
        temporary_path.unlink()
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_json(hasher: Any, payload: object) -> None:
    hasher.update(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    )
    hasher.update(b"\n")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _datetime_text(value: datetime) -> str:
    return as_utc(value, field="datetime").isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return as_utc(parsed, field="datetime")
