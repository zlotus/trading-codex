import fcntl
import json
import os
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_codex.baostock_download.constants import (
    BLACKLIST_RULES_SHA256,
    PROJECT_CALENDAR_DAY_HARD_LIMIT,
    PROVIDER,
    PROVIDER_CALENDAR_DAY_HARD_LIMIT,
)
from trading_codex.baostock_download.errors import (
    BudgetExceeded,
    ProviderLockError,
    StateError,
)
from trading_codex.baostock_download.manifest import RequestLimits, canonical_json

SHANGHAI = ZoneInfo("Asia/Shanghai")
TERMINAL_SESSION_EVENTS = ("completed", "failed", "blacklisted", "abandoned")

SCHEMA = """
CREATE TABLE metadata (
    provider TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    provider_calendar_day_hard_limit INTEGER NOT NULL,
    project_calendar_day_hard_limit INTEGER NOT NULL,
    provider_rules_sha256 TEXT NOT NULL
);

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL,
    data_root TEXT NOT NULL,
    started_at TEXT NOT NULL
);

CREATE TABLE session_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    manifest_sha256 TEXT NOT NULL,
    item_id TEXT,
    kind TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    calendar_day TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);

CREATE TABLE attempt_results (
    result_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    status TEXT NOT NULL,
    provider_code TEXT,
    message TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE item_events (
    event_id TEXT PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL,
    item_id TEXT NOT NULL,
    event TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE incidents (
    incident_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    session_id TEXT REFERENCES sessions(session_id),
    attempt_id TEXT REFERENCES attempts(attempt_id),
    detected_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE recoveries (
    recovery_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL UNIQUE REFERENCES incidents(incident_id),
    operator TEXT NOT NULL,
    administrator_confirmation TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX attempts_started_at_idx ON attempts(started_at);
CREATE INDEX attempts_calendar_day_idx ON attempts(calendar_day);
CREATE INDEX attempts_manifest_item_idx ON attempts(manifest_sha256, item_id);
CREATE INDEX item_events_item_idx ON item_events(manifest_sha256, item_id, recorded_at);
"""

AUDIT_TABLES = (
    "metadata",
    "sessions",
    "session_events",
    "attempts",
    "attempt_results",
    "item_events",
    "incidents",
    "recoveries",
)


def _trigger_sql(table: str) -> str:
    return f"""
CREATE TRIGGER {table}_no_update
BEFORE UPDATE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} is append-only');
END;
CREATE TRIGGER {table}_no_delete
BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} is append-only');
END;
"""


@dataclass(frozen=True)
class BudgetSnapshot:
    calendar_day: str
    calendar_day_attempts: int
    rolling_24h_attempts: int
    provider_day_remaining: int
    project_day_remaining: int
    configured_day_remaining: int
    configured_rolling_remaining: int

    def as_dict(self) -> dict[str, int | str]:
        return self.__dict__


class GlobalProviderLock:
    def __init__(self, state_root: Path) -> None:
        self.path = state_root / "provider.lock"
        self._handle: object | None = None

    def __enter__(self) -> "GlobalProviderLock":
        if not self.path.parent.is_dir():
            raise StateError("global state root is missing; run doctor --initialize")
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ProviderLockError("another BaoStock fetch owns the global provider lock") from exc
        owner = canonical_json({"pid": os.getpid(), "acquired_at": _utc_text(_now())})
        handle.seek(0)
        handle.truncate()
        handle.write(owner)
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is None:
            return
        handle = self._handle
        assert hasattr(handle, "fileno") and hasattr(handle, "close")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._handle = None


class StateStore:
    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = root.resolve()
        self.path = self.root / "request-audit.sqlite"
        self.clock = clock or _now
        self.sleep = sleep

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.inspect()
            return
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(SCHEMA)
            for table in AUDIT_TABLES:
                connection.executescript(_trigger_sql(table))
            connection.execute(
                """
                INSERT INTO metadata (
                    provider, schema_version, created_at,
                    provider_calendar_day_hard_limit,
                    project_calendar_day_hard_limit,
                    provider_rules_sha256
                ) VALUES (?, 1, ?, ?, ?, ?)
                """,
                (
                    PROVIDER,
                    _utc_text(self.clock()),
                    PROVIDER_CALENDAR_DAY_HARD_LIMIT,
                    PROJECT_CALENDAR_DAY_HARD_LIMIT,
                    BLACKLIST_RULES_SHA256,
                ),
            )
            connection.commit()
        except Exception:
            connection.close()
            raise
        finally:
            if connection:
                connection.close()
        lock_path = self.root / "provider.lock"
        with lock_path.open("ab") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(self.root)
        self.inspect()

    def inspect(self) -> dict[str, object]:
        connection = self._connect()
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise StateError(f"global state integrity check failed: {integrity}")
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise StateError("global state foreign key check failed")
            metadata = connection.execute(
                "SELECT * FROM metadata WHERE provider = ?", (PROVIDER,)
            ).fetchone()
            if metadata is None:
                raise StateError("global state provider metadata is missing")
            expected = (
                1,
                PROVIDER_CALENDAR_DAY_HARD_LIMIT,
                PROJECT_CALENDAR_DAY_HARD_LIMIT,
            )
            actual = (
                metadata["schema_version"],
                metadata["provider_calendar_day_hard_limit"],
                metadata["project_calendar_day_hard_limit"],
                metadata["provider_rules_sha256"],
            )
            expected = (*expected, BLACKLIST_RULES_SHA256)
            if actual != expected:
                raise StateError("global state provider identity or hard limits changed")
            trigger_count = connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE '%_no_%'
                """
            ).fetchone()[0]
            if trigger_count != len(AUDIT_TABLES) * 2:
                raise StateError("global state append-only triggers are incomplete")
            return {
                "status": "passed",
                "provider": PROVIDER,
                "schema_version": 1,
                "database": str(self.path),
                "integrity": "ok",
                "append_only_triggers": trigger_count,
                "provider_rules_sha256": BLACKLIST_RULES_SHA256,
            }
        except sqlite3.DatabaseError as exc:
            raise StateError("global state database is unreadable") from exc
        finally:
            connection.close()

    def assert_fetch_ready(self, *, now: datetime | None = None) -> None:
        boundary = _aware(now or self.clock())
        self.inspect()
        connection = self._connect()
        try:
            self._assert_clock(connection, boundary)
            unresolved = connection.execute(
                f"""
                SELECT s.session_id
                FROM sessions AS s
                WHERE NOT EXISTS (
                    SELECT 1 FROM session_events AS e
                    WHERE e.session_id = s.session_id
                      AND e.event IN ({','.join('?' for _ in TERMINAL_SESSION_EVENTS)})
                )
                ORDER BY s.started_at
                """,
                TERMINAL_SESSION_EVENTS,
            ).fetchall()
            if unresolved:
                ids = ", ".join(row["session_id"] for row in unresolved[:3])
                raise StateError(f"unclosed fetch session requires manual recovery: {ids}")
            incident = self._active_blacklist(connection)
            if incident is not None:
                raise StateError(
                    "provider_blacklisted hard stop is active; administrator-confirmed "
                    f"recovery is required for incident {incident['incident_id']}"
                )
        finally:
            connection.close()

    def start_session(
        self,
        *,
        manifest_sha256: str,
        data_root: Path,
        now: datetime | None = None,
    ) -> str:
        recorded_at = _aware(now or self.clock())
        session_id = f"session-{uuid.uuid4()}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_clock(connection, recorded_at)
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    manifest_sha256,
                    str(data_root.resolve()),
                    _utc_text(recorded_at),
                ),
            )
            self._insert_session_event(
                connection,
                session_id=session_id,
                event="started",
                recorded_at=recorded_at,
                detail={},
            )
            connection.commit()
            return session_id
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise StateError("failed to start append-only fetch session") from exc
        finally:
            connection.close()

    def append_session_event(
        self,
        session_id: str,
        event: str,
        *,
        detail: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> None:
        recorded_at = _aware(now or self.clock())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_session_event(
                connection,
                session_id=session_id,
                event=event,
                recorded_at=recorded_at,
                detail=detail or {},
            )
            connection.commit()
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise StateError("failed to append fetch session event") from exc
        finally:
            connection.close()

    def append_item_event(
        self,
        *,
        manifest_sha256: str,
        item_id: str,
        event: str,
        detail: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> None:
        recorded_at = _aware(now or self.clock())
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO item_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"item-event-{uuid.uuid4()}",
                    manifest_sha256,
                    item_id,
                    event,
                    _utc_text(recorded_at),
                    canonical_json(detail or {}).decode().strip(),
                ),
            )
            connection.commit()
        except sqlite3.DatabaseError as exc:
            raise StateError("failed to append manifest item event") from exc
        finally:
            connection.close()

    def item_statuses(self, manifest_sha256: str) -> dict[str, dict[str, object]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT item_id, event, recorded_at, detail_json
                FROM item_events
                WHERE manifest_sha256 = ?
                ORDER BY rowid
                """,
                (manifest_sha256,),
            ).fetchall()
            result = {}
            for row in rows:
                result[row["item_id"]] = {
                    "event": row["event"],
                    "recorded_at": row["recorded_at"],
                    "detail": json.loads(row["detail_json"]),
                }
            return result
        finally:
            connection.close()

    def item_attempt_count(self, manifest_sha256: str, item_id: str) -> int:
        connection = self._connect()
        try:
            return connection.execute(
                """
                SELECT COUNT(*) FROM attempts
                WHERE manifest_sha256 = ? AND item_id = ?
                """,
                (manifest_sha256, item_id),
            ).fetchone()[0]
        finally:
            connection.close()

    def cooldown_remaining(
        self,
        *,
        minimum_interval_seconds: float,
        now: datetime | None = None,
    ) -> float:
        boundary = _aware(now or self.clock())
        connection = self._connect()
        try:
            self._assert_clock(connection, boundary)
            return _cooldown_remaining(
                connection,
                now=boundary,
                minimum_interval_seconds=minimum_interval_seconds,
            )
        finally:
            connection.close()

    def wait_for_cooldown(self, limits: RequestLimits) -> None:
        remaining = self.cooldown_remaining(
            minimum_interval_seconds=limits.minimum_interval_seconds
        )
        if remaining > 0:
            self.sleep(remaining)

    def assert_capacity(
        self,
        *,
        additional_attempts: int,
        limits: RequestLimits,
        now: datetime | None = None,
        session_id: str | None = None,
    ) -> None:
        if additional_attempts < 1:
            raise ValueError("additional_attempts must be positive")
        boundary = _aware(now or self.clock())
        connection = self._connect()
        try:
            self._assert_clock(connection, boundary)
            counts = self._counts(connection, boundary)
            _check_global_capacity(counts, additional_attempts, limits)
            if session_id is not None:
                session_count = connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
                if session_count + additional_attempts > limits.session_attempts:
                    raise BudgetExceeded("session attempt budget is insufficient")
        finally:
            connection.close()

    def reserve_attempt(
        self,
        *,
        session_id: str,
        manifest_sha256: str,
        item_id: str | None,
        kind: str,
        limits: RequestLimits,
        item_attempt_limit: int | None = None,
        now: datetime | None = None,
    ) -> str:
        recorded_at = _aware(now or self.clock())
        attempt_id = f"attempt-{uuid.uuid4()}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_clock(connection, recorded_at)
            self._require_open_session(connection, session_id)
            cooldown = _cooldown_remaining(
                connection,
                now=recorded_at,
                minimum_interval_seconds=limits.minimum_interval_seconds,
            )
            if cooldown > 0:
                raise StateError(
                    "minimum request interval has not elapsed "
                    f"({cooldown:.3f} seconds remaining)"
                )
            counts = self._counts(connection, recorded_at)
            _check_global_capacity(counts, 1, limits)
            session_count = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            if session_count >= limits.session_attempts:
                raise BudgetExceeded("session attempt budget exhausted")
            if item_id is not None:
                if item_attempt_limit is None:
                    raise StateError("item attempts require an explicit item budget")
                item_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM attempts
                    WHERE manifest_sha256 = ? AND item_id = ?
                    """,
                    (manifest_sha256, item_id),
                ).fetchone()[0]
                if item_count >= item_attempt_limit:
                    raise BudgetExceeded(f"item attempt budget exhausted for {item_id}")
            connection.execute(
                """
                INSERT INTO attempts (
                    attempt_id, session_id, manifest_sha256, item_id, kind,
                    sequence, started_at, calendar_day
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    session_id,
                    manifest_sha256,
                    item_id,
                    kind,
                    session_count + 1,
                    _utc_text(recorded_at),
                    recorded_at.astimezone(SHANGHAI).date().isoformat(),
                ),
            )
            connection.commit()
            return attempt_id
        except BudgetExceeded:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise StateError("failed to reserve socket attempt") from exc
        finally:
            connection.close()

    def record_attempt_result(
        self,
        attempt_id: str,
        *,
        status: str,
        provider_code: str | None = None,
        message: str | None = None,
        now: datetime | None = None,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO attempt_results VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"attempt-result-{uuid.uuid4()}",
                    attempt_id,
                    status,
                    provider_code,
                    message,
                    _utc_text(_aware(now or self.clock())),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            raise StateError("socket attempt already has a terminal result") from exc
        except sqlite3.DatabaseError as exc:
            raise StateError("failed to append socket attempt result") from exc
        finally:
            connection.close()

    def record_blacklist(
        self,
        *,
        session_id: str,
        attempt_id: str,
        detail: dict[str, object],
        now: datetime | None = None,
    ) -> str:
        incident_id = f"incident-{uuid.uuid4()}"
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?)",
                (
                    incident_id,
                    "provider_blacklisted",
                    session_id,
                    attempt_id,
                    _utc_text(_aware(now or self.clock())),
                    canonical_json(detail).decode().strip(),
                ),
            )
            connection.commit()
            return incident_id
        except sqlite3.DatabaseError as exc:
            raise StateError("failed to persist provider blacklist incident") from exc
        finally:
            connection.close()

    def recover_blacklist(
        self,
        *,
        incident_id: str,
        operator: str,
        administrator_confirmation: str,
        reason: str,
        now: datetime | None = None,
    ) -> str:
        if not all(value.strip() for value in (operator, administrator_confirmation, reason)):
            raise StateError("blacklist recovery requires operator, confirmation, and reason")
        recovery_id = f"recovery-{uuid.uuid4()}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            incident = connection.execute(
                "SELECT kind FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if incident is None or incident["kind"] != "provider_blacklisted":
                raise StateError("blacklist incident does not exist")
            connection.execute(
                "INSERT INTO recoveries VALUES (?, ?, ?, ?, ?, ?)",
                (
                    recovery_id,
                    incident_id,
                    operator.strip(),
                    administrator_confirmation.strip(),
                    reason.strip(),
                    _utc_text(_aware(now or self.clock())),
                ),
            )
            connection.commit()
            return recovery_id
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise StateError("blacklist incident already has a recovery event") from exc
        finally:
            connection.close()

    def abandon_session(
        self,
        *,
        session_id: str,
        operator: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        if not operator.strip() or not reason.strip():
            raise StateError("session recovery requires operator and reason")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_open_session(connection, session_id)
            self._insert_session_event(
                connection,
                session_id=session_id,
                event="abandoned",
                recorded_at=_aware(now or self.clock()),
                detail={"operator": operator.strip(), "reason": reason.strip()},
            )
            connection.commit()
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise StateError("failed to append session recovery event") from exc
        finally:
            connection.close()

    def budget_snapshot(
        self,
        limits: RequestLimits,
        *,
        now: datetime | None = None,
    ) -> BudgetSnapshot:
        boundary = _aware(now or self.clock())
        connection = self._connect()
        try:
            counts = self._counts(connection, boundary)
            return BudgetSnapshot(
                calendar_day=boundary.astimezone(SHANGHAI).date().isoformat(),
                calendar_day_attempts=counts[0],
                rolling_24h_attempts=counts[1],
                provider_day_remaining=max(
                    0, PROVIDER_CALENDAR_DAY_HARD_LIMIT - counts[0]
                ),
                project_day_remaining=max(
                    0, PROJECT_CALENDAR_DAY_HARD_LIMIT - counts[0]
                ),
                configured_day_remaining=max(0, limits.calendar_day_attempts - counts[0]),
                configured_rolling_remaining=max(
                    0, limits.rolling_24h_attempts - counts[1]
                ),
            )
        finally:
            connection.close()

    def mirror_to(self, destination: Path) -> None:
        if destination.resolve() == self.path:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent, suffix=".sqlite.tmp"
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            source = self._connect()
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
                source.close()
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        except sqlite3.DatabaseError as exc:
            raise StateError("failed to mirror global request audit") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise StateError("global state database is missing; run doctor --initialize")
        try:
            connection = sqlite3.connect(self.path, timeout=0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except sqlite3.DatabaseError as exc:
            raise StateError("cannot open global state database") from exc

    @staticmethod
    def _assert_clock(connection: sqlite3.Connection, now: datetime) -> None:
        row = connection.execute(
            "SELECT started_at FROM attempts ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row is not None and _parse_utc(row["started_at"]) > now + timedelta(seconds=5):
            raise StateError("system clock moved backwards relative to the attempt ledger")

    @staticmethod
    def _active_blacklist(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT i.incident_id
            FROM incidents AS i
            LEFT JOIN recoveries AS r ON r.incident_id = i.incident_id
            WHERE i.kind = 'provider_blacklisted' AND r.recovery_id IS NULL
            ORDER BY i.detected_at
            LIMIT 1
            """
        ).fetchone()

    @staticmethod
    def _counts(connection: sqlite3.Connection, now: datetime) -> tuple[int, int]:
        calendar_day = now.astimezone(SHANGHAI).date().isoformat()
        rolling_start = _utc_text(now - timedelta(hours=24))
        day_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE calendar_day = ?", (calendar_day,)
        ).fetchone()[0]
        rolling_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE started_at > ?", (rolling_start,)
        ).fetchone()[0]
        return day_count, rolling_count

    @staticmethod
    def _require_open_session(connection: sqlite3.Connection, session_id: str) -> None:
        session = connection.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise StateError(f"fetch session does not exist: {session_id}")
        terminal = connection.execute(
            f"""
            SELECT 1 FROM session_events
            WHERE session_id = ?
              AND event IN ({','.join('?' for _ in TERMINAL_SESSION_EVENTS)})
            LIMIT 1
            """,
            (session_id, *TERMINAL_SESSION_EVENTS),
        ).fetchone()
        if terminal is not None:
            raise StateError(f"fetch session is already terminal: {session_id}")

    @staticmethod
    def _insert_session_event(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        event: str,
        recorded_at: datetime,
        detail: dict[str, object],
    ) -> None:
        connection.execute(
            "INSERT INTO session_events VALUES (?, ?, ?, ?, ?)",
            (
                f"session-event-{uuid.uuid4()}",
                session_id,
                event,
                _utc_text(recorded_at),
                canonical_json(detail).decode().strip(),
            ),
        )


def _check_global_capacity(
    counts: tuple[int, int],
    additional: int,
    limits: RequestLimits,
) -> None:
    day_count, rolling_count = counts
    _check_provider_capacity(day_count, additional)
    if day_count + additional > PROJECT_CALENDAR_DAY_HARD_LIMIT:
        raise BudgetExceeded("project 45,000/calendar-day hard limit would be exceeded")
    if day_count + additional > limits.calendar_day_attempts:
        raise BudgetExceeded("configured calendar-day attempt budget would be exceeded")
    if rolling_count + additional > limits.rolling_24h_attempts:
        raise BudgetExceeded("configured rolling-24-hour attempt budget would be exceeded")


def _cooldown_remaining(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    minimum_interval_seconds: float,
) -> float:
    row = connection.execute(
        "SELECT started_at FROM attempts ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return 0.0
    elapsed = (now - _parse_utc(row["started_at"])).total_seconds()
    return max(0.0, minimum_interval_seconds - elapsed)


def _check_provider_capacity(day_count: int, additional: int) -> None:
    if day_count + additional > PROVIDER_CALENDAR_DAY_HARD_LIMIT:
        raise BudgetExceeded("BaoStock 50,000/IP/calendar-day hard limit would be exceeded")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StateError("state clock must return a timezone-aware timestamp")
    return value.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc_text(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError("attempt ledger contains an invalid timestamp") from exc
    return _aware(parsed)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
