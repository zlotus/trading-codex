import fcntl
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from zoneinfo import ZoneInfo

from trading_codex.baostock_download.constants import (
    DEFAULT_DAILY_ATTEMPT_STOP,
    PROVIDER_CALENDAR_DAY_HARD_LIMIT,
)
from trading_codex.baostock_download.errors import (
    DailyAttemptLimitReached,
    ProviderBlacklisted,
    ProviderLockError,
)
from trading_codex.baostock_download.requests import canonical_json

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class AttemptSnapshot:
    calendar_day: str
    attempts: int
    stop_at: int
    official_limit: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "calendar_day": self.calendar_day,
            "attempts": self.attempts,
            "remaining": max(0, self.stop_at - self.attempts),
            "stop_at": self.stop_at,
            "official_limit": self.official_limit,
        }


class GlobalDownloadLock:
    """One non-blocking process lock shared by every data root for this user."""

    def __init__(self, state_root: Path) -> None:
        self.path = state_root / "provider.lock"
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "GlobalDownloadLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ProviderLockError("another BaoStock downloader process is running") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(
            canonical_json(
                {
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
            )
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class DailyAttemptCounter:
    """A plain append-only daily text counter, written before every socket send."""

    def __init__(
        self,
        state_root: Path,
        *,
        stop_at: int = DEFAULT_DAILY_ATTEMPT_STOP,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if stop_at < 2 or stop_at > PROVIDER_CALENDAR_DAY_HARD_LIMIT:
            raise ValueError(
                f"daily stop must be between 2 and {PROVIDER_CALENDAR_DAY_HARD_LIMIT}"
            )
        self.state_root = state_root.resolve()
        self.stop_at = stop_at
        self.clock = clock or (lambda: datetime.now(UTC))
        self._cached_day: str | None = None
        self._cached_count = 0

    @property
    def blacklist_marker(self) -> Path:
        return self.state_root / "provider-blacklisted.json"

    def assert_not_blacklisted(self) -> None:
        if self.blacklist_marker.exists():
            raise ProviderBlacklisted(
                "local BaoStock blacklist marker is present; confirm provider recovery "
                f"before removing {self.blacklist_marker}"
            )

    def mark_blacklisted(self, detail: dict[str, object]) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        if self.blacklist_marker.exists():
            return
        payload = canonical_json(
            {
                "detected_at": self._now().isoformat().replace("+00:00", "Z"),
                **detail,
            }
        )
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.state_root, suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.blacklist_marker)
            _fsync_directory(self.state_root)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def reserve(self, *, kind: str, request_id: str | None) -> int:
        now = self._now()
        day = now.astimezone(SHANGHAI).date().isoformat()
        self._refresh(day)
        # Keep the final slot available for a clean logout. If a paginated
        # response reaches this boundary, the caller closes the local socket.
        boundary = self.stop_at if kind == "logout" else self.stop_at - 1
        if self._cached_count >= boundary:
            raise DailyAttemptLimitReached(
                f"BaoStock daily attempt stop reached ({self.stop_at:,})"
            )
        path = self._log_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json(
            {
                "at": now.isoformat().replace("+00:00", "Z"),
                "kind": kind,
                "request_id": request_id,
            }
        )
        with path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._cached_count += 1
        return self._cached_count

    def snapshot(self) -> AttemptSnapshot:
        now = self._now()
        day = now.astimezone(SHANGHAI).date().isoformat()
        self._refresh(day)
        return AttemptSnapshot(
            calendar_day=day,
            attempts=self._cached_count,
            stop_at=self.stop_at,
            official_limit=PROVIDER_CALENDAR_DAY_HARD_LIMIT,
        )

    def _refresh(self, day: str) -> None:
        if self._cached_day == day:
            return
        self.state_root.mkdir(parents=True, exist_ok=True)
        log_count = 0
        path = self._log_path(day)
        if path.is_file():
            with path.open("rb") as handle:
                log_count = sum(1 for line in handle if line.strip())
        self._cached_day = day
        self._cached_count = log_count + self._legacy_count(day)

    def _legacy_count(self, day: str) -> int:
        """Include attempts recorded by the superseded SQLite downloader."""
        legacy = self.state_root / "request-audit.sqlite"
        if not legacy.is_file():
            return 0
        try:
            connection = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE calendar_day = ?", (day,)
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"cannot read legacy BaoStock request counter: {legacy}") from exc
        return int(row[0]) if row is not None else 0

    def _log_path(self, day: str) -> Path:
        return self.state_root / "attempts" / f"{day}.jsonl"

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("attempt counter clock must be timezone-aware")
        return value.astimezone(UTC)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
