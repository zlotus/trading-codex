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
from trading_codex.data.normalizers import NORMALIZERS, normalize_batch
from trading_codex.data.parquet_store import PROVENANCE_COLUMNS, ParquetDataStore
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
    active_dataset: str | None = None

    for current in _verified(raw_root, warnings=warnings):
        valid_raw_files += 1
        try:
            dataset = NORMALIZERS[current.batch.operation][0]
        except KeyError:
            warnings.append(
                _warning(
                    current.artifact.path,
                    f"normalization failed: unsupported provider operation: "
                    f"{current.batch.operation}",
                )
            )
            continue
        if dataset != active_dataset:
            existing_hashes.clear()
            existing_keys.clear()
            active_dataset = dataset
        report = reports.setdefault(
            dataset,
            {
                "raw_files": 0,
                "published_segments": 0,
                "rows": 0,
                "deduplicated_rows": 0,
                "conflicting_rows": 0,
                "skipped": 0,
            },
        )
        report["raw_files"] += 1
        if dataset in blocked_datasets:
            report["skipped"] += 1
            continue
        destination = store.segment_path(dataset, current.artifact.content_sha256)
        if destination.is_file():
            try:
                physical_schema = pq.read_schema(destination)
            except (pa.ArrowException, OSError) as exc:
                warnings.append(
                    _warning(destination, f"cannot read normalized {dataset}: {exc}")
                )
                blocked_datasets.add(dataset)
            else:
                if physical_schema != DATASET_SPECS[dataset].schema:
                    warnings.append(
                        _warning(destination, f"schema mismatch for normalized {dataset}")
                    )
                    blocked_datasets.add(dataset)
            report["skipped"] += 1
            continue
        if dataset not in existing_hashes:
            try:
                spec = DATASET_SPECS[dataset]
                existing = store.read_columns(
                    dataset,
                    columns=(*spec.keys, "source_payload_sha256"),
                )
            except DataValidationError as exc:
                warnings.append(
                    _warning(normalized_root, f"cannot read normalized {dataset}: {exc}")
                )
                blocked_datasets.add(dataset)
                report["skipped"] += 1
                continue
            hashes, keys = _existing_index(existing, spec.keys)
            existing_hashes[dataset] = hashes
            existing_keys[dataset] = keys
        if current.artifact.content_sha256 in existing_hashes[dataset]:
            report["skipped"] += 1
            continue
        try:
            normalized_dataset, rows = normalize_batch(current.batch, current.artifact)
        except (DataValidationError, ValueError) as exc:
            warnings.append(_warning(current.artifact.path, f"normalization failed: {exc}"))
            continue
        if normalized_dataset != dataset:
            raise RuntimeError("normalizer dataset mapping changed during ingest")
        if not rows:
            report["skipped"] += 1
            continue
        spec = DATASET_SPECS[dataset]
        keyed_rows = [
            (tuple(row[column] for column in spec.keys), row) for row in rows
        ]
        incoming_keys = [key for key, _row in keyed_rows]
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
            conflicts = _conflicting_overlap_keys(
                store,
                dataset=dataset,
                keyed_rows=keyed_rows,
                overlap=overlap,
            )
            if conflicts:
                report["conflicting_rows"] += len(conflicts)
                warnings.append(
                    _warning(
                        current.artifact.path,
                        f"normalized business keys conflict with existing {dataset} rows; "
                        f"raw payload was not published ({len(conflicts)} conflicts)",
                    )
                )
                report["skipped"] += 1
                continue
            report["deduplicated_rows"] += len(overlap)
            keyed_rows = [
                (key, row)
                for key, row in keyed_rows
                if key not in existing_keys[dataset]
            ]
            if not keyed_rows:
                report["skipped"] += 1
                continue
            rows = [row for _key, row in keyed_rows]
            unique_incoming_keys = {key for key, _row in keyed_rows}
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
    for path in sorted(provider_root.glob("*/*.json"), key=_raw_path_sort_key):
        try:
            yield verify_envelope(path, raw_root=raw_root)
        except RawIntegrityError as exc:
            warnings.append(_warning(path, str(exc)))


def _raw_path_sort_key(path: Path) -> tuple[str, str, str]:
    operation = path.parent.name
    try:
        dataset = NORMALIZERS[operation][0]
    except KeyError:
        dataset = f"~{operation}"
    return dataset, operation, path.name


def _warning(path: Path, reason: str) -> dict[str, str]:
    return {"path": str(path), "reason": reason}


def _existing_index(
    table: pa.Table,
    key_columns: tuple[str, ...],
) -> tuple[set[str], set[tuple[object, ...]]]:
    hashes: set[str] = set()
    keys: set[tuple[object, ...]] = set()
    for batch in table.to_batches(max_chunksize=65_536):
        hashes.update(batch.column("source_payload_sha256").to_pylist())
        values = [batch.column(column).to_pylist() for column in key_columns]
        keys.update(zip(*values, strict=True))
    return hashes, keys


def _conflicting_overlap_keys(
    store: ParquetDataStore,
    *,
    dataset: str,
    keyed_rows: list[tuple[tuple[object, ...], dict[str, object]]],
    overlap: set[tuple[object, ...]],
) -> set[tuple[object, ...]]:
    spec = DATASET_SPECS[dataset]
    business_columns = tuple(
        column for column in spec.schema.names if column not in PROVENANCE_COLUMNS
    )
    accepted = {
        column: tuple({key[index] for key in overlap})
        for index, column in enumerate(spec.keys)
    }
    existing = store.read_columns(
        dataset,
        columns=business_columns,
        contained_in=accepted,
    )
    existing_values = {
        tuple(row[column] for column in spec.keys): tuple(
            row[column] for column in business_columns
        )
        for row in existing.to_pylist()
        if tuple(row[column] for column in spec.keys) in overlap
    }
    incoming_values = {
        key: tuple(row[column] for column in business_columns)
        for key, row in keyed_rows
        if key in overlap
    }
    return {
        key
        for key in overlap
        if key not in existing_values or existing_values[key] != incoming_values[key]
    }


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
