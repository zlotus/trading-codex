import fcntl
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from trading_codex.baostock_download.constants import (
    FAIL_FREE_BYTES,
    MAX_USED_PERCENT,
    PEAK_RESERVE_BYTES,
    WARN_FREE_BYTES,
)
from trading_codex.baostock_download.errors import StoragePreflightError

LAYOUT_DIRECTORIES = (
    "raw",
    "normalized",
    "state",
    "manifests/draft",
    "manifests/frozen",
    "manifests/completed",
    "reports/data-quality",
    "reports/coverage",
    "reports/backfill",
    "quarantine",
    "tmp",
    "backup-manifests",
)


@dataclass(frozen=True)
class StorageThresholds:
    warn_free_bytes: int = WARN_FREE_BYTES
    fail_free_bytes: int = FAIL_FREE_BYTES
    peak_reserve_bytes: int = PEAK_RESERVE_BYTES
    max_used_percent: float = MAX_USED_PERCENT


@dataclass(frozen=True)
class StorageReport:
    data_root: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "passed",
            "data_root": self.data_root,
            "space": {
                "total_bytes": self.total_bytes,
                "used_bytes": self.used_bytes,
                "free_bytes": self.free_bytes,
                "used_percent": self.used_percent,
            },
            "warnings": list(self.warnings),
        }


class StorageGuard:
    def __init__(
        self,
        *,
        data_root: Path,
        thresholds: StorageThresholds | None = None,
    ) -> None:
        self.data_root = data_root.resolve()
        self.thresholds = thresholds or StorageThresholds()

    def preflight(
        self,
        *,
        initialize: bool = False,
        estimated_peak_bytes: int = 0,
    ) -> StorageReport:
        if estimated_peak_bytes < 0:
            raise ValueError("estimated_peak_bytes must be non-negative")
        if initialize:
            self.data_root.mkdir(parents=True, exist_ok=True)
            for relative in LAYOUT_DIRECTORIES:
                (self.data_root / relative).mkdir(parents=True, exist_ok=True)
        elif not self.data_root.is_dir():
            raise StoragePreflightError("data root does not exist; run doctor --initialize")

        self._probe_durable_writes()
        usage = shutil.disk_usage(self.data_root)
        used_percent = (usage.used / usage.total * 100.0) if usage.total else 100.0
        if usage.free < self.thresholds.fail_free_bytes:
            raise StoragePreflightError(
                f"free space is below {self.thresholds.fail_free_bytes} bytes"
            )
        if used_percent >= self.thresholds.max_used_percent:
            raise StoragePreflightError(
                f"storage use is {used_percent:.2f}%, at or above the "
                f"{self.thresholds.max_used_percent:.2f}% stop boundary"
            )
        required_free = (
            self.thresholds.fail_free_bytes
            + self.thresholds.peak_reserve_bytes
            + estimated_peak_bytes
        )
        if usage.free < required_free:
            raise StoragePreflightError(
                "estimated peak use plus the storage reserve would cross the free-space boundary"
            )
        warnings = ()
        if usage.free < self.thresholds.warn_free_bytes:
            warnings = (
                f"free space is below the warning boundary of "
                f"{self.thresholds.warn_free_bytes} bytes",
            )
        return StorageReport(
            data_root=str(self.data_root),
            total_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
            used_percent=used_percent,
            warnings=warnings,
        )

    def _probe_durable_writes(self) -> None:
        probe_directory = self.data_root / "tmp" / ".storage-probe"
        probe_directory.mkdir(parents=True, exist_ok=True)
        original = probe_directory / "original"
        replacement = probe_directory / "replacement"
        lock_path = probe_directory / "lock"
        try:
            _write_and_fsync(original, b"original\n")
            _write_and_fsync(replacement, b"replacement\n")
            os.replace(replacement, original)
            _fsync_directory(probe_directory)
            if original.read_bytes() != b"replacement\n":
                raise StoragePreflightError("atomic replace probe returned unexpected data")
            self._probe_lock(lock_path)
        except StoragePreflightError:
            raise
        except OSError as exc:
            raise StoragePreflightError("storage write/fsync/replace probe failed") from exc
        finally:
            for path in (replacement, original, lock_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            try:
                probe_directory.rmdir()
                _fsync_directory(probe_directory.parent)
            except OSError:
                pass

    @staticmethod
    def _probe_lock(path: Path) -> None:
        first = path.open("a+b")
        second = path.open("a+b")
        try:
            fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            raise StoragePreflightError("filesystem locks are not process-exclusive")
        finally:
            fcntl.flock(first.fileno(), fcntl.LOCK_UN)
            first.close()
            second.close()


def _write_and_fsync(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
