import os
import tempfile
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from trading_codex.data.models import DataValidationError, FutureDataError, MergeResult
from trading_codex.data.schemas import DATASET_SPECS, DatasetSpec
from trading_codex.data.time import SHANGHAI, require_aware

PROVENANCE_COLUMNS = {
    "available_at",
    "source",
    "source_received_at",
    "source_payload_sha256",
    "raw_artifact",
}


class ParquetDataStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, dataset: str) -> Path:
        self._spec(dataset)
        return self.root / f"{dataset}.parquet"

    def read(self, dataset: str) -> pa.Table:
        spec = self._spec(dataset)
        path = self.path_for(dataset)
        if not path.exists():
            return pa.Table.from_pylist([], schema=spec.schema)
        table = pq.read_table(path, schema=spec.schema)
        if table.schema != spec.schema:
            raise DataValidationError(f"schema mismatch for normalized dataset {dataset}")
        return table

    def merge(self, dataset: str, rows: Iterable[dict[str, Any]]) -> MergeResult:
        spec = self._spec(dataset)
        incoming_rows = list(rows)
        if not incoming_rows:
            total = self.read(dataset).num_rows
            return MergeResult(dataset, 0, 0, 0, 0, total)

        incoming_table = self._table_from_rows(spec, incoming_rows)
        existing_table = self.read(dataset)
        existing = {self._key(row, spec): row for row in existing_table.to_pylist()}

        inserted = 0
        updated = 0
        unchanged = 0
        for row in incoming_table.to_pylist():
            key = self._key(row, spec)
            current = existing.get(key)
            if current is None:
                existing[key] = row
                inserted += 1
            elif self._business_values(current) == self._business_values(row):
                unchanged += 1
            else:
                existing[key] = row
                updated += 1

        merged = self._table_from_rows(spec, existing.values()).sort_by(
            [(key, "ascending") for key in spec.keys]
        )
        if inserted or updated:
            self._write_atomic(self.path_for(dataset), merged)

        return MergeResult(
            dataset=dataset,
            incoming=len(incoming_rows),
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
            total=merged.num_rows,
        )

    def rows_as_of(self, dataset: str, *, as_of: datetime) -> list[dict[str, Any]]:
        boundary = require_aware(as_of, field="as_of")
        table = self.read(dataset)
        if table.num_rows == 0:
            return []
        mask = pc.less_equal(table["available_at"], pa.scalar(boundary))
        return table.filter(mask).to_pylist()

    def daily_bars(
        self,
        *,
        codes: Iterable[str],
        start_date: date,
        end_date: date,
        as_of: datetime,
        adjustment_flag: str = "3",
    ) -> list[dict[str, Any]]:
        boundary = require_aware(as_of, field="as_of")
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        if end_date > boundary.astimezone(SHANGHAI).date():
            raise FutureDataError("daily-bar end_date exceeds as_of")
        code_set = set(codes)
        return [
            row
            for row in self.rows_as_of("daily_bars", as_of=boundary)
            if row["code"] in code_set
            and start_date <= row["trade_date"] <= end_date
            and row["adjustment_flag"] == adjustment_flag
        ]

    def five_minute_bars(
        self,
        *,
        codes: Iterable[str],
        start_date: date,
        end_date: date,
        as_of: datetime,
        adjustment_flag: str = "3",
    ) -> list[dict[str, Any]]:
        boundary = require_aware(as_of, field="as_of")
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        if end_date > boundary.astimezone(SHANGHAI).date():
            raise FutureDataError("five-minute-bar end_date exceeds as_of")
        code_set = set(codes)
        return [
            row
            for row in self.rows_as_of("five_minute_bars", as_of=boundary)
            if row["code"] in code_set
            and start_date <= row["trade_date"] <= end_date
            and row["adjustment_flag"] == adjustment_flag
        ]

    @staticmethod
    def _key(row: dict[str, Any], spec: DatasetSpec) -> tuple[Any, ...]:
        return tuple(row[column] for column in spec.keys)

    @staticmethod
    def _business_values(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key not in PROVENANCE_COLUMNS}

    @staticmethod
    def _table_from_rows(spec: DatasetSpec, rows: Iterable[dict[str, Any]]) -> pa.Table:
        try:
            return pa.Table.from_pylist(list(rows), schema=spec.schema)
        except (pa.ArrowException, TypeError, ValueError) as exc:
            raise DataValidationError(f"invalid rows for normalized dataset {spec.name}") from exc

    @staticmethod
    def _write_atomic(path: Path, table: pa.Table) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".tmp", delete=False
            ) as handle:
                temporary_path = Path(handle.name)
            pq.write_table(table, temporary_path, compression="zstd")
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _spec(dataset: str) -> DatasetSpec:
        try:
            return DATASET_SPECS[dataset]
        except KeyError as exc:
            raise ValueError(f"unknown normalized dataset: {dataset}") from exc
