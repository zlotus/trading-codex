import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_codex.data.models import FutureDataError
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.point_in_time import PointInTimeCoverageReport
from trading_codex.data.schemas import DATASET_SPECS
from trading_codex.data.time import SHANGHAI, require_aware


@dataclass(frozen=True)
class QualityIssue:
    dataset: str
    severity: str
    message: str


@dataclass(frozen=True)
class DatasetQuality:
    rows: int
    duplicate_keys: int
    missing_provenance: int


@dataclass(frozen=True)
class DataQualityReport:
    generated_at: datetime
    as_of: datetime
    status: str
    datasets: dict[str, DatasetQuality]
    issues: tuple[QualityIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "as_of": self.as_of,
            "status": self.status,
            "datasets": {name: asdict(summary) for name, summary in self.datasets.items()},
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class OpeningCoverageReport:
    generated_at: datetime
    as_of: datetime
    status: str
    start_date: date
    end_date: date
    calendar_complete: bool
    universe_complete: bool
    expected_code_days: int
    covered_code_days: int
    coverage_ratio: float | None
    by_code: dict[str, dict[str, int | float | None]]
    missing_calendar_dates: tuple[str, ...]
    missing_universe_dates: tuple[str, ...]
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_data_quality(
    store: ParquetDataStore,
    *,
    as_of: datetime,
    generated_at: datetime | None = None,
) -> DataQualityReport:
    boundary = require_aware(as_of, field="as_of")
    generated = require_aware(generated_at or datetime.now(UTC), field="generated_at")
    issues: list[QualityIssue] = []
    summaries: dict[str, DatasetQuality] = {}
    rows_by_dataset: dict[str, list[dict[str, Any]]] = {}

    for name, spec in DATASET_SPECS.items():
        rows = store.rows_as_of(name, as_of=boundary)
        rows_by_dataset[name] = rows
        keys = [tuple(row[column] for column in spec.keys) for row in rows]
        duplicate_keys = len(keys) - len(set(keys))
        missing_provenance = sum(
            any(
                not row.get(column)
                for column in (
                    "source",
                    "source_received_at",
                    "source_payload_sha256",
                    "raw_artifact",
                )
            )
            for row in rows
        )
        summaries[name] = DatasetQuality(
            rows=len(rows),
            duplicate_keys=duplicate_keys,
            missing_provenance=missing_provenance,
        )
        if duplicate_keys:
            issues.append(QualityIssue(name, "error", f"{duplicate_keys} duplicate primary keys"))
        if missing_provenance:
            issues.append(
                QualityIssue(name, "error", f"{missing_provenance} rows lack provenance")
            )

    calendar = {
        row["calendar_date"]: row["is_trading_day"]
        for row in rows_by_dataset["trade_calendar"]
    }
    for row in rows_by_dataset["daily_bars"]:
        if calendar and calendar.get(row["trade_date"]) is not True:
            issues.append(
                QualityIssue(
                    "daily_bars",
                    "error",
                    f"{row['code']} has a bar on non-trading date {row['trade_date']}",
                )
            )
        if row["trade_status"] and not _valid_ohlc(row):
            issues.append(
                QualityIssue(
                    "daily_bars",
                    "error",
                    f"{row['code']} has invalid OHLC on {row['trade_date']}",
                )
            )

    for row in rows_by_dataset["five_minute_bars"]:
        if not _valid_ohlc(row):
            issues.append(
                QualityIssue(
                    "five_minute_bars",
                    "error",
                    f"{row['code']} has invalid OHLC at {row['timestamp'].isoformat()}",
                )
            )

    status = "failed" if any(issue.severity == "error" for issue in issues) else "passed"
    return DataQualityReport(
        generated_at=generated,
        as_of=boundary,
        status=status,
        datasets=summaries,
        issues=tuple(issues),
    )


def assess_opening_0935_coverage(
    store: ParquetDataStore,
    *,
    codes: list[str],
    start_date: date,
    end_date: date,
    as_of: datetime,
    generated_at: datetime | None = None,
) -> OpeningCoverageReport:
    boundary = require_aware(as_of, field="as_of")
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    if end_date > boundary.astimezone(SHANGHAI).date():
        raise FutureDataError("coverage end_date exceeds as_of")
    code_set = set(codes)
    if not code_set:
        raise ValueError("at least one instrument code is required")
    requested_dates = {
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    }
    calendar_rows = [
        row
        for row in store.rows_as_of("trade_calendar", as_of=boundary)
        if row["calendar_date"] in requested_dates
    ]
    calendar_dates = {row["calendar_date"] for row in calendar_rows}
    missing_calendar_dates = sorted(requested_dates - calendar_dates)
    trading_days = {
        row["calendar_date"]
        for row in calendar_rows
        if row["is_trading_day"]
    }
    universe_rows = [
        row
        for row in store.rows_as_of("historical_universe", as_of=boundary)
        if row["snapshot_date"] in trading_days
    ]
    universe_dates = {row["snapshot_date"] for row in universe_rows}
    missing_universe_dates = sorted(trading_days - universe_dates)
    universe_pairs = {
        (row["snapshot_date"], row["code"])
        for row in universe_rows
        if row["trade_status"]
        and row["code"] in code_set
        and row["snapshot_date"] in trading_days
    }
    expected = universe_pairs | {
        (day, code) for day in missing_universe_dates for code in code_set
    }
    opening_pairs = {
        (row["trade_date"], row["code"])
        for row in store.rows_as_of("five_minute_bars", as_of=boundary)
        if row["code"] in code_set
        and start_date <= row["trade_date"] <= end_date
        and row["adjustment_flag"] == "3"
        and row["timestamp"].astimezone(SHANGHAI).time() == time(9, 35)
    }
    covered = expected & opening_pairs
    missing_pairs = sorted(expected - opening_pairs)
    status = (
        "passed"
        if not missing_calendar_dates
        and not missing_universe_dates
        and not missing_pairs
        else "failed"
    )
    by_code: dict[str, dict[str, int | float | None]] = {}
    for code in sorted(code_set):
        expected_count = sum(pair[1] == code for pair in expected)
        covered_count = sum(pair[1] == code for pair in covered)
        by_code[code] = {
            "expected": expected_count,
            "covered": covered_count,
            "coverage_ratio": covered_count / expected_count if expected_count else None,
        }

    return OpeningCoverageReport(
        generated_at=require_aware(
            generated_at or datetime.now(UTC), field="generated_at"
        ),
        as_of=boundary,
        status=status,
        start_date=start_date,
        end_date=end_date,
        calendar_complete=not missing_calendar_dates,
        universe_complete=not missing_universe_dates,
        expected_code_days=len(expected),
        covered_code_days=len(covered),
        coverage_ratio=len(covered) / len(expected) if expected else None,
        by_code=by_code,
        missing_calendar_dates=tuple(day.isoformat() for day in missing_calendar_dates),
        missing_universe_dates=tuple(day.isoformat() for day in missing_universe_dates),
        missing=tuple(f"{day.isoformat()}:{code}" for day, code in missing_pairs),
    )


def write_report(
    report: DataQualityReport | OpeningCoverageReport | PointInTimeCoverageReport,
    directory: Path,
) -> Path:
    payload = _json_bytes(report.as_dict())
    digest = hashlib.sha256(payload).hexdigest()[:16]
    if isinstance(report, DataQualityReport):
        kind = "data-quality"
    elif isinstance(report, OpeningCoverageReport):
        kind = "opening-0935-coverage"
    else:
        kind = "point-in-time-coverage"
    path = directory / f"{kind}-{digest}.json"
    if path.exists():
        return path
    directory.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return path


def _valid_ohlc(row: dict[str, Any]) -> bool:
    values = [row.get(field) for field in ("open", "high", "low", "close")]
    if any(value is None for value in values):
        return False
    open_price, high, low, close = values
    return (
        all(value > Decimal(0) for value in values)
        and high >= max(open_price, low, close)
        and low <= min(open_price, high, close)
    )


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            default=_json_value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _json_value(value: Any) -> str:
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if not isinstance(value, Decimal) else str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")
