import fcntl
import hashlib
import os
import shutil
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from trading_codex.baostock_download.errors import (
    OfflineSyncError,
    SchemaDriftError,
)
from trading_codex.baostock_download.manifest import (
    Manifest,
    canonical_json,
    durable_write,
)
from trading_codex.baostock_download.state import StateStore
from trading_codex.data.models import (
    DataValidationError,
    ProviderBatch,
    RawArtifact,
    RawIntegrityError,
)
from trading_codex.data.normalizers import normalize_batch
from trading_codex.data.parquet_store import PROVENANCE_COLUMNS, ParquetDataStore
from trading_codex.data.raw_store import ImmutableRawStore
from trading_codex.data.schemas import DATASET_SPECS, DatasetSpec

MAX_SEGMENT_ROWS = 1_000_000
MAX_SEGMENT_BYTES = 2 * 1024**3


def manifest_status(
    *,
    data_root: Path,
    manifest: Manifest,
    state: StateStore,
) -> dict[str, object]:
    raw_store = ImmutableRawStore(data_root / "raw")
    recorded = state.item_statuses(manifest.manifest_sha256)
    items = []
    complete = 0
    for item in manifest.items:
        cached = raw_store.lookup_with_artifact(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
        )
        status = recorded.get(item.item_id, {}).get("event", "planned")
        raw_artifact = None
        if cached is not None:
            batch, artifact = cached
            if batch.fields != item.expected_fields:
                status = "schema_drift"
            else:
                status = "raw_committed" if status == "planned" else status
                raw_artifact = artifact.relative_path
                complete += 1
        items.append(
            {
                "item_id": item.item_id,
                "operation": item.operation,
                "status": status,
                "raw_artifact": raw_artifact,
                "attempts": state.item_attempt_count(
                    manifest.manifest_sha256, item.item_id
                ),
            }
        )
    return {
        "status": "complete" if complete == len(manifest.items) else "incomplete",
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.manifest_sha256,
        "items_total": len(manifest.items),
        "items_with_raw": complete,
        "items": items,
        "budget": state.budget_snapshot(manifest.limits).as_dict(),
    }


def sync_manifest(
    *,
    data_root: Path,
    manifest: Manifest,
    state: StateStore,
) -> dict[str, object]:
    with data_root_lock(data_root):
        return _sync_manifest_unlocked(
            data_root=data_root,
            manifest=manifest,
            state=state,
        )


def _sync_manifest_unlocked(
    *,
    data_root: Path,
    manifest: Manifest,
    state: StateStore,
) -> dict[str, object]:
    raw_store = ImmutableRawStore(data_root / "raw")
    normalized_store = ParquetDataStore(data_root / "normalized")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    item_artifacts: dict[str, RawArtifact] = {}
    for item in manifest.items:
        cached = raw_store.lookup_with_artifact(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
        )
        if cached is None:
            raise OfflineSyncError(f"raw cache miss for manifest item {item.item_id}")
        batch, artifact = cached
        if batch.fields != item.expected_fields:
            raise SchemaDriftError(
                f"cached {item.operation} fields differ from the frozen contract"
            )
        try:
            dataset, rows = normalize_batch(batch, artifact)
        except DataValidationError as exc:
            quarantine = _write_normalization_quarantine(
                data_root=data_root,
                manifest=manifest,
                item_id=item.item_id,
                artifact=artifact,
                reason=str(exc),
            )
            state.append_item_event(
                manifest_sha256=manifest.manifest_sha256,
                item_id=item.item_id,
                event="quarantined",
                detail={"reason": "normalization_failed", "path": str(quarantine)},
            )
            raise OfflineSyncError(
                f"normalization failed for manifest item {item.item_id}"
            ) from exc
        grouped[dataset].extend(rows)
        item_artifacts[item.item_id] = artifact

    staging_root = data_root / "tmp" / f"sync-{manifest.manifest_sha256}"
    staging_root.mkdir(parents=True, exist_ok=True)
    staged: dict[str, Path] = {}
    reports = []
    try:
        for dataset, incoming in sorted(grouped.items()):
            spec = DATASET_SPECS[dataset]
            existing = normalized_store.read(dataset).to_pylist()
            merged, inserted_rows, counts, conflicts = _strict_merge(
                spec, existing, incoming
            )
            if conflicts:
                quarantine = _write_conflict_quarantine(
                    data_root=data_root,
                    manifest=manifest,
                    dataset=dataset,
                    conflicts=conflicts,
                )
                raise OfflineSyncError(
                    f"normalized key conflict in {dataset}; quarantined at {quarantine}"
                )
            segment = None
            if inserted_rows:
                if len(inserted_rows) > MAX_SEGMENT_ROWS:
                    raise OfflineSyncError(
                        f"{dataset} segment exceeds the {MAX_SEGMENT_ROWS}-row bound; "
                        "split the frozen manifest"
                    )
                path = staging_root / f"{dataset}.parquet"
                table = _table(spec, inserted_rows)
                pq.write_table(table, path, compression="zstd")
                _fsync_file(path)
                if path.stat().st_size > MAX_SEGMENT_BYTES:
                    raise OfflineSyncError(
                        f"{dataset} segment exceeds the {MAX_SEGMENT_BYTES}-byte bound; "
                        "split the frozen manifest"
                    )
                _fsync_directory(path.parent)
                staged[dataset] = path
                segment = str(
                    normalized_store.segment_path(
                        dataset, manifest.manifest_sha256
                    ).relative_to(data_root)
                )
            reports.append(
                {
                    "dataset": dataset,
                    "incoming": len(incoming),
                    "inserted": counts[0],
                    "unchanged": counts[1],
                    "total": len(merged),
                    "segment": segment,
                }
            )

        for dataset, staging_path in sorted(staged.items()):
            destination = normalized_store.segment_path(
                dataset, manifest.manifest_sha256
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise OfflineSyncError(
                    f"refusing to replace published normalized segment: {destination}"
                )
            os.replace(staging_path, destination)
            _fsync_directory(destination.parent)
        for item in manifest.items:
            artifact = item_artifacts[item.item_id]
            state.append_item_event(
                manifest_sha256=manifest.manifest_sha256,
                item_id=item.item_id,
                event="normalized",
                detail={"content_sha256": artifact.content_sha256},
            )
    except OSError as exc:
        raise OfflineSyncError("normalized staging or atomic segment publish failed") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    return {
        "status": "passed",
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.manifest_sha256,
        "datasets": reports,
    }


def verify_manifest(
    *,
    data_root: Path,
    manifest: Manifest,
    state: StateStore,
    as_of: datetime,
) -> dict[str, object]:
    with data_root_lock(data_root):
        return _verify_manifest_unlocked(
            data_root=data_root,
            manifest=manifest,
            state=state,
            as_of=as_of,
        )


def _verify_manifest_unlocked(
    *,
    data_root: Path,
    manifest: Manifest,
    state: StateStore,
    as_of: datetime,
) -> dict[str, object]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    boundary = as_of.astimezone(UTC)
    raw_store = ImmutableRawStore(data_root / "raw")
    normalized_store = ParquetDataStore(data_root / "normalized")
    issues: list[str] = []
    expected_hashes: dict[str, set[str]] = defaultdict(set)
    expected_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    recorded_statuses = state.item_statuses(manifest.manifest_sha256)
    items = []
    for item in manifest.items:
        cached = raw_store.lookup_with_artifact(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
        )
        if cached is None:
            issues.append(f"missing_raw:{item.item_id}")
            items.append({"item_id": item.item_id, "status": "missing_raw"})
            continue
        batch, artifact = cached
        if batch.fields != item.expected_fields:
            issues.append(f"schema_drift:{item.item_id}")
            items.append({"item_id": item.item_id, "status": "schema_drift"})
            continue
        try:
            dataset, rows = normalize_batch(batch, artifact)
        except DataValidationError as exc:
            issues.append(f"normalization_failed:{item.item_id}:{exc}")
            items.append({"item_id": item.item_id, "status": "normalization_failed"})
            continue
        if any(row["available_at"] > boundary for row in rows):
            issues.append(f"future_data:{item.item_id}")
        if rows:
            expected_hashes[dataset].add(artifact.content_sha256)
            expected_rows[dataset].extend(_table(DATASET_SPECS[dataset], rows).to_pylist())
        recorded = recorded_statuses.get(item.item_id, {}).get("event")
        if recorded not in {"normalized", "completed"}:
            issues.append(f"item_not_normalized:{item.item_id}")
        items.append(
            {
                "item_id": item.item_id,
                "status": "verified_raw",
                "raw_artifact": artifact.relative_path,
                "content_sha256": artifact.content_sha256,
                "rows": len(rows),
                "dataset": dataset,
            }
        )

    dataset_reports = []
    for dataset, hashes in sorted(expected_hashes.items()):
        try:
            rows = normalized_store.read(dataset).to_pylist()
        except DataValidationError as exc:
            issues.append(f"normalized_schema:{dataset}:{exc}")
            continue
        spec = DATASET_SPECS[dataset]
        seen = set()
        duplicates = 0
        actual_by_key = {}
        for row in rows:
            key = tuple(row[field] for field in spec.keys)
            if key in seen:
                duplicates += 1
            seen.add(key)
            actual_by_key[key] = row
            if any(not row.get(field) for field in PROVENANCE_COLUMNS - {"available_at"}):
                issues.append(f"invalid_provenance:{dataset}")
                break
        present_hashes = {row["source_payload_sha256"] for row in rows}
        missing_hashes = sorted(hashes - present_hashes)
        missing_rows = 0
        mismatched_rows = 0
        for expected in expected_rows[dataset]:
            key = _key(expected, spec)
            actual = actual_by_key.get(key)
            if actual is None:
                missing_rows += 1
            elif actual != expected:
                mismatched_rows += 1
        if duplicates:
            issues.append(f"duplicate_keys:{dataset}:{duplicates}")
        if missing_hashes:
            issues.append(f"missing_normalized_payloads:{dataset}:{','.join(missing_hashes)}")
        if missing_rows:
            issues.append(f"missing_normalized_rows:{dataset}:{missing_rows}")
        if mismatched_rows:
            issues.append(f"mismatched_normalized_rows:{dataset}:{mismatched_rows}")
        dataset_reports.append(
            {
                "dataset": dataset,
                "rows": len(rows),
                "required_payloads": len(hashes),
                "missing_payloads": missing_hashes,
                "duplicate_keys": duplicates,
                "missing_rows": missing_rows,
                "mismatched_rows": mismatched_rows,
            }
        )

    quarantine_root = data_root / "quarantine" / manifest.manifest_id
    quarantined = (
        sorted(str(path) for path in quarantine_root.rglob("*.json"))
        if quarantine_root.exists()
        else []
    )
    if quarantined:
        issues.append(f"quarantine_present:{len(quarantined)}")
    status = "passed" if not issues else "failed"
    report = {
        "status": status,
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.manifest_sha256,
        "as_of": boundary.isoformat().replace("+00:00", "Z"),
        "items": items,
        "datasets": dataset_reports,
        "issues": issues,
        "quarantine": quarantined,
    }
    report_payload = canonical_json(report)
    report_sha256 = hashlib.sha256(report_payload).hexdigest()
    report_path = (
        data_root
        / "reports"
        / "backfill"
        / f"verify-{manifest.manifest_id}-{report_sha256}.json"
    )
    _write_immutable(report_path, report_payload, description="verification report")
    report["report"] = str(report_path)
    if status == "passed":
        receipt = {
            "schema_version": 1,
            "manifest_id": manifest.manifest_id,
            "manifest_sha256": manifest.manifest_sha256,
            "verified_as_of": report["as_of"],
            "report": str(report_path.relative_to(data_root)),
        }
        receipt_path = (
            data_root
            / "manifests"
            / "completed"
            / f"{manifest.manifest_id}.receipt.json"
        )
        _write_immutable(
            receipt_path,
            canonical_json(receipt),
            description="manifest completion receipt",
        )
        report["completion_receipt"] = str(receipt_path)
        for item in manifest.items:
            state.append_item_event(
                manifest_sha256=manifest.manifest_sha256,
                item_id=item.item_id,
                event="completed",
                detail={"report": str(report_path)},
            )
    return report


def import_raw_cache(
    *,
    source_root: Path,
    data_root: Path,
    source_provider_client_version: str,
) -> dict[str, object]:
    if source_provider_client_version != "00.9.30":
        raise OfflineSyncError(
            "source raw cache must be explicitly attributed to BaoStock client 00.9.30"
        )
    with data_root_lock(data_root):
        return _import_raw_cache_unlocked(
            source_root=source_root,
            data_root=data_root,
            source_provider_client_version=source_provider_client_version,
        )


def _import_raw_cache_unlocked(
    *,
    source_root: Path,
    data_root: Path,
    source_provider_client_version: str,
) -> dict[str, object]:
    source = ImmutableRawStore(source_root.resolve())
    destination = ImmutableRawStore(data_root / "raw")
    imported = 0
    existing = 0
    existing_paths = {
        path.resolve() for path in destination.root.glob("baostock/*/*.json")
    }
    try:
        verified = sorted(
            source.iter_verified(source="baostock"),
            key=lambda item: (item[0].received_at, item[1].relative_path),
        )
    except RawIntegrityError as exc:
        raise OfflineSyncError("source raw cache failed integrity verification") from exc
    for batch, _ in verified:
        recorded_version = batch.query.get("_provider_client_version")
        if recorded_version not in {None, source_provider_client_version}:
            raise OfflineSyncError(
                "source raw cache records a different BaoStock client version"
            )
        versioned_query = {
            **batch.query,
            "_provider_client_version": source_provider_client_version,
        }
        versioned = ProviderBatch(
            source=batch.source,
            operation=batch.operation,
            query=versioned_query,
            fields=batch.fields,
            rows=batch.rows,
            received_at=batch.received_at,
        )
        artifact = destination.persist(versioned)
        resolved = artifact.path.resolve()
        if resolved not in existing_paths:
            imported += 1
            existing_paths.add(resolved)
        else:
            existing += 1
    return {
        "status": "passed",
        "source_root": str(source.root),
        "data_root": str(data_root.resolve()),
        "imported": imported,
        "existing": existing,
    }


@contextmanager
def data_root_lock(data_root: Path):
    path = data_root / "state" / "data-root.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OfflineSyncError(
                "another fetch, status, sync, verify, raw import, or recovery "
                "owns the data-root lock"
            ) from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _strict_merge(
    spec: DatasetSpec,
    existing_rows: list[dict[str, Any]],
    incoming_rows: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    tuple[int, int],
    list[dict[str, Any]],
]:
    merged = {_key(row, spec): row for row in existing_rows}
    conflicts = []
    if len(merged) != len(existing_rows):
        conflicts.append(
            {
                "reason": "existing_duplicate_keys",
                "existing_rows": len(existing_rows),
                "unique_keys": len(merged),
            }
        )
    inserted = 0
    unchanged = 0
    inserted_rows = []
    for incoming in _table(spec, incoming_rows).to_pylist():
        key = _key(incoming, spec)
        current = merged.get(key)
        if current is None:
            merged[key] = incoming
            inserted += 1
            inserted_rows.append(incoming)
        elif _business_values(current) == _business_values(incoming):
            unchanged += 1
        else:
            conflicts.append(
                {"key": list(key), "existing": current, "incoming": incoming}
            )
    ordered = sorted(merged.values(), key=lambda row: _sortable_key(row, spec))
    inserted_rows.sort(key=lambda row: _sortable_key(row, spec))
    return ordered, inserted_rows, (inserted, unchanged), conflicts


def _table(spec: DatasetSpec, rows: list[dict[str, Any]]) -> pa.Table:
    try:
        return pa.Table.from_pylist(rows, schema=spec.schema)
    except (pa.ArrowException, TypeError, ValueError) as exc:
        raise OfflineSyncError(
            f"invalid normalized rows for {spec.name}: {exc}"
        ) from exc


def _key(row: dict[str, Any], spec: DatasetSpec) -> tuple[Any, ...]:
    return tuple(row[field] for field in spec.keys)


def _sortable_key(row: dict[str, Any], spec: DatasetSpec) -> tuple[str, ...]:
    return tuple(str(row[field]) for field in spec.keys)


def _business_values(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in PROVENANCE_COLUMNS}


def _write_normalization_quarantine(
    *,
    data_root: Path,
    manifest: Manifest,
    item_id: str,
    artifact: RawArtifact,
    reason: str,
) -> Path:
    payload = {
        "schema_version": 1,
        "reason": "normalization_failed",
        "message": reason,
        "manifest_sha256": manifest.manifest_sha256,
        "item_id": item_id,
        "raw_artifact": artifact.relative_path,
        "content_sha256": artifact.content_sha256,
    }
    return _write_quarantine(data_root, manifest, payload)


def _write_conflict_quarantine(
    *,
    data_root: Path,
    manifest: Manifest,
    dataset: str,
    conflicts: list[dict[str, Any]],
) -> Path:
    payload = {
        "schema_version": 1,
        "reason": "normalized_key_conflict",
        "manifest_sha256": manifest.manifest_sha256,
        "dataset": dataset,
        "conflicts": _jsonable(conflicts),
    }
    return _write_quarantine(data_root, manifest, payload)


def _write_quarantine(
    data_root: Path,
    manifest: Manifest,
    payload: dict[str, Any],
) -> Path:
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    path = data_root / "quarantine" / manifest.manifest_id / f"{digest}.json"
    durable_write(path, canonical_json({**payload, "content_sha256": digest}))
    return path


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if not isinstance(value, Decimal) else str(value)
    return value


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable(path: Path, payload: bytes, *, description: str) -> None:
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise OfflineSyncError(f"cannot read existing {description}: {path}") from exc
        if existing == payload:
            return
        raise OfflineSyncError(f"refusing to replace existing {description}: {path}")
    durable_write(path, payload)
