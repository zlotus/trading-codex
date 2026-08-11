import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trading_codex.baostock_download.envelope import (
    VerifiedEnvelope,
    verify_envelope,
)
from trading_codex.data.models import DataValidationError, RawIntegrityError
from trading_codex.data.normalizers import normalize_batch
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.schemas import DATASET_SPECS


def inspect_raw_envelopes(data_root: Path) -> dict[str, object]:
    raw_root = data_root.resolve() / "raw"
    warnings: list[dict[str, str]] = []
    valid = sum(1 for _ in _verified(raw_root, warnings=warnings))
    return {
        "status": "passed" if not warnings else "warnings",
        "network_access": False,
        "raw_root": str(raw_root),
        "files": valid + len(warnings),
        "valid": valid,
        "warnings": warnings,
    }


def ingest_raw_envelopes(data_root: Path) -> dict[str, object]:
    root = data_root.resolve()
    raw_root = root / "raw"
    normalized_root = root / "normalized"
    store = ParquetDataStore(normalized_root)
    warnings: list[dict[str, str]] = []
    valid_raw_files = 0
    existing_hashes: dict[str, set[str]] = {}
    existing_keys: dict[str, set[tuple[object, ...]]] = {}
    blocked_datasets: set[str] = set()
    reports: dict[str, dict[str, int]] = {}

    for current in _verified(raw_root, warnings=warnings):
        valid_raw_files += 1
        try:
            dataset, rows = normalize_batch(current.batch, current.artifact)
        except (DataValidationError, ValueError) as exc:
            warnings.append(_warning(current.artifact.path, f"normalization failed: {exc}"))
            continue
        report = reports.setdefault(
            dataset,
            {"raw_files": 0, "published_segments": 0, "rows": 0, "skipped": 0},
        )
        report["raw_files"] += 1
        if dataset in blocked_datasets:
            report["skipped"] += 1
            continue
        if dataset not in existing_hashes:
            try:
                existing = store.read(dataset).to_pylist()
            except DataValidationError as exc:
                warnings.append(
                    _warning(normalized_root, f"cannot read normalized {dataset}: {exc}")
                )
                blocked_datasets.add(dataset)
                report["skipped"] += 1
                continue
            existing_hashes[dataset] = {
                row["source_payload_sha256"] for row in existing
            }
            spec = DATASET_SPECS[dataset]
            existing_keys[dataset] = {
                tuple(row[column] for column in spec.keys) for row in existing
            }
        if current.artifact.content_sha256 in existing_hashes[dataset]:
            report["skipped"] += 1
            continue
        if not rows:
            report["skipped"] += 1
            continue
        spec = DATASET_SPECS[dataset]
        incoming_keys = [tuple(row[column] for column in spec.keys) for row in rows]
        unique_incoming_keys = set(incoming_keys)
        if len(unique_incoming_keys) != len(incoming_keys):
            warnings.append(
                _warning(
                    current.artifact.path,
                    f"normalization produced duplicate business keys for {dataset}",
                )
            )
            report["skipped"] += 1
            continue
        overlap = unique_incoming_keys & existing_keys[dataset]
        if overlap:
            warnings.append(
                _warning(
                    current.artifact.path,
                    f"normalized business keys overlap existing {dataset} rows",
                )
            )
            report["skipped"] += 1
            continue
        destination = store.segment_path(dataset, current.artifact.content_sha256)
        if destination.is_file():
            report["skipped"] += 1
            continue
        try:
            table = pa.Table.from_pylist(rows, schema=DATASET_SPECS[dataset].schema)
            _write_parquet_atomic(destination, table)
        except (pa.ArrowException, OSError, TypeError, ValueError) as exc:
            warnings.append(
                _warning(current.artifact.path, f"cannot publish normalized segment: {exc}")
            )
            continue
        existing_hashes[dataset].add(current.artifact.content_sha256)
        existing_keys[dataset].update(unique_incoming_keys)
        report["published_segments"] += 1
        report["rows"] += len(rows)

    return {
        "status": "passed" if not warnings else "warnings",
        "network_access": False,
        "raw_root": str(raw_root),
        "normalized_root": str(normalized_root),
        "valid_raw_files": valid_raw_files,
        "datasets": [
            {"dataset": dataset, **report}
            for dataset, report in sorted(reports.items())
        ],
        "warnings": warnings,
    }


def _verified(
    raw_root: Path,
    *,
    warnings: list[dict[str, str]],
) -> Iterator[VerifiedEnvelope]:
    provider_root = raw_root / "baostock"
    if not provider_root.is_dir():
        warnings.append(_warning(provider_root, "raw provider directory does not exist"))
        return
    for path in sorted(provider_root.glob("*/*.json")):
        try:
            yield verify_envelope(path, raw_root=raw_root)
        except RawIntegrityError as exc:
            warnings.append(_warning(path, str(exc)))


def _warning(path: Path, reason: str) -> dict[str, str]:
    return {"path": str(path), "reason": reason}


def _write_parquet_atomic(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
        pq.write_table(table, temporary, compression="zstd")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
