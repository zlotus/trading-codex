import os
import tempfile
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
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
        self._validated_files: dict[Path, tuple[int, int]] = {}

    def path_for(self, dataset: str) -> Path:
        self._spec(dataset)
        return self.root / f"{dataset}.parquet"

    def read(self, dataset: str) -> pa.Table:
        spec = self._spec(dataset)
        path = self.path_for(dataset)
        paths = ([path] if path.exists() else []) + self.segment_paths(dataset)
        if not paths:
            return pa.Table.from_pylist([], schema=spec.schema)
        tables = []
        for current in paths:
            try:
                physical_schema = pq.read_schema(current)
            except (pa.ArrowException, OSError) as exc:
                raise DataValidationError(
                    f"cannot read normalized dataset {dataset}: {current}"
                ) from exc
            if physical_schema != spec.schema:
                raise DataValidationError(
                    f"schema mismatch for normalized dataset {dataset}: {current}"
                )
            try:
                table = pq.read_table(current)
            except (pa.ArrowException, OSError) as exc:
                raise DataValidationError(
                    f"cannot read normalized dataset {dataset}: {current}"
                ) from exc
            if table.schema != spec.schema:
                raise DataValidationError(
                    f"schema mismatch for normalized dataset {dataset}: {current}"
                )
            tables.append(table)
        table = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
        if table.num_rows:
            table = table.sort_by([(key, "ascending") for key in spec.keys])
            keys = [self._key(row, spec) for row in table.to_pylist()]
            if len(keys) != len(set(keys)):
                raise DataValidationError(
                    f"duplicate business keys in normalized dataset {dataset}"
                )
        return table

    def scan(
        self,
        dataset: str,
        *,
        as_of: datetime,
        columns: Iterable[str] | None = None,
        equal: dict[str, Any] | None = None,
        contained_in: dict[str, Iterable[Any]] | None = None,
        ranges: dict[str, tuple[Any | None, Any | None]] | None = None,
    ) -> pa.Table:
        """Read a point-in-time subset with Arrow predicate and column pushdown."""
        spec = self._spec(dataset)
        boundary = require_aware(as_of, field="as_of")
        requested = self._requested_columns(spec, columns)
        expression = ds.field("available_at") <= pa.scalar(boundary)
        expression = self._scan_expression(
            spec,
            expression,
            equal=equal or {},
            contained_in=contained_in or {},
            ranges=ranges or {},
        )
        scan_columns = tuple(dict.fromkeys((*requested, *spec.keys)))
        tables = list(
            self._filtered_tables(
                dataset,
                expression=expression,
                columns=scan_columns,
            )
        )
        if not tables:
            return pa.Table.from_pylist([], schema=self._projected_schema(spec, requested))
        table = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
        if table.num_rows:
            table = table.sort_by([(key, "ascending") for key in spec.keys])
            self._ensure_unique_sorted_keys(table, spec.keys, dataset=dataset)
        return table.select(requested)

    def read_columns(
        self,
        dataset: str,
        *,
        columns: Iterable[str],
        equal: dict[str, Any] | None = None,
        contained_in: dict[str, Iterable[Any]] | None = None,
        ranges: dict[str, tuple[Any | None, Any | None]] | None = None,
    ) -> pa.Table:
        """Read a projection with optional filters and full business-key checks."""
        spec = self._spec(dataset)
        requested = self._requested_columns(spec, columns)
        scan_columns = tuple(dict.fromkeys((*requested, *spec.keys)))
        expression = self._scan_expression(
            spec,
            ds.scalar(True),
            equal=equal or {},
            contained_in=contained_in or {},
            ranges=ranges or {},
        )
        tables = list(
            self._filtered_tables(
                dataset,
                expression=expression,
                columns=scan_columns,
            )
        )
        if not tables:
            return pa.Table.from_pylist([], schema=self._projected_schema(spec, requested))
        table = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
        if table.num_rows:
            table = table.sort_by([(key, "ascending") for key in spec.keys])
            self._ensure_unique_sorted_keys(table, spec.keys, dataset=dataset)
        return table.select(requested)

    def daily_bar_series(
        self,
        *,
        codes: Iterable[str],
        start_date: date,
        end_date: date,
        as_of: datetime,
        adjustment_flags: Iterable[str] = ("3",),
        columns: Iterable[str] | None = None,
    ) -> dict[tuple[str, str], pa.Table]:
        """Return bounded daily-bar tables grouped by ``(code, adjustment_flag)``."""
        spec = self._spec("daily_bars")
        boundary = require_aware(as_of, field="as_of")
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        if end_date > boundary.astimezone(SHANGHAI).date():
            raise FutureDataError("daily-bar end_date exceeds as_of")
        code_values = tuple(sorted(set(codes)))
        flag_values = tuple(sorted(set(adjustment_flags)))
        requested = self._requested_columns(spec, columns)
        if not code_values or not flag_values:
            return {}

        required = ("trade_date", "code", "adjustment_flag")
        scan_columns = tuple(dict.fromkeys((*requested, *required)))
        expression = (
            (ds.field("available_at") <= pa.scalar(boundary))
            & ds.field("code").isin(code_values)
            & ds.field("adjustment_flag").isin(flag_values)
            & (ds.field("trade_date") >= pa.scalar(start_date))
            & (ds.field("trade_date") <= pa.scalar(end_date))
        )
        grouped: dict[tuple[str, str], list[pa.Table]] = {}
        for table in self._filtered_tables(
            "daily_bars",
            expression=expression,
            columns=scan_columns,
        ):
            if table.num_rows == 0:
                continue
            codes_in_table = pc.unique(table["code"]).to_pylist()
            flags_in_table = pc.unique(table["adjustment_flag"]).to_pylist()
            for code in codes_in_table:
                for flag in flags_in_table:
                    selected = table.filter(
                        pc.and_(
                            pc.equal(table["code"], pa.scalar(code)),
                            pc.equal(table["adjustment_flag"], pa.scalar(flag)),
                        )
                    )
                    if selected.num_rows:
                        grouped.setdefault((code, flag), []).append(selected)

        result: dict[tuple[str, str], pa.Table] = {}
        for key, parts in sorted(grouped.items()):
            table = pa.concat_tables(parts) if len(parts) > 1 else parts[0]
            table = table.sort_by([("trade_date", "ascending")])
            self._ensure_unique_sorted_keys(
                table,
                required,
                dataset="daily_bars",
            )
            result[key] = table.select(requested)
        return result

    def segment_path(self, dataset: str, segment_id: str) -> Path:
        self._spec(dataset)
        if not segment_id or any(character not in "0123456789abcdef" for character in segment_id):
            raise ValueError("segment_id must contain lowercase hexadecimal characters")
        return self.root / ".segments" / dataset / f"{segment_id}.parquet"

    def segment_paths(self, dataset: str) -> list[Path]:
        self._spec(dataset)
        directory = self.root / ".segments" / dataset
        return sorted(directory.glob("*.parquet")) if directory.is_dir() else []

    def merge(self, dataset: str, rows: Iterable[dict[str, Any]]) -> MergeResult:
        spec = self._spec(dataset)
        if self.segment_paths(dataset):
            raise DataValidationError(
                f"normalized dataset {dataset} uses immutable segments; "
                "publish through trading-codex-data ingest-raw"
            )
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
        return self.scan(dataset, as_of=as_of).to_pylist()

    def daily_bars(
        self,
        *,
        codes: Iterable[str],
        start_date: date,
        end_date: date,
        as_of: datetime,
        adjustment_flag: str = "3",
    ) -> list[dict[str, Any]]:
        series = self.daily_bar_series(
            codes=codes,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
            adjustment_flags=(adjustment_flag,),
        )
        if not series:
            return []
        table = pa.concat_tables(list(series.values()))
        return table.sort_by(
            [(key, "ascending") for key in self._spec("daily_bars").keys]
        ).to_pylist()

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

    def _filtered_tables(
        self,
        dataset: str,
        *,
        expression: ds.Expression,
        columns: tuple[str, ...],
    ) -> Iterable[pa.Table]:
        spec = self._spec(dataset)
        paths = self._dataset_paths(dataset)
        if not paths:
            return
        self._validate_files(dataset, paths)
        arrow_dataset = ds.dataset(
            [str(path) for path in paths],
            format="parquet",
            schema=spec.schema,
        )
        for fragment in arrow_dataset.get_fragments(filter=expression):
            try:
                table = fragment.to_table(columns=list(columns), filter=expression)
            except (pa.ArrowException, OSError) as exc:
                raise DataValidationError(
                    f"cannot scan normalized dataset {dataset}: {fragment.path}"
                ) from exc
            if table.num_rows:
                yield table

    def _dataset_paths(self, dataset: str) -> list[Path]:
        path = self.path_for(dataset)
        return ([path] if path.exists() else []) + self.segment_paths(dataset)

    def _validate_files(self, dataset: str, paths: Iterable[Path]) -> None:
        spec = self._spec(dataset)
        for path in paths:
            try:
                stat = path.stat()
                identity = (stat.st_mtime_ns, stat.st_size)
                if self._validated_files.get(path) == identity:
                    continue
                physical_schema = pq.read_schema(path)
            except (pa.ArrowException, OSError) as exc:
                raise DataValidationError(
                    f"cannot read normalized dataset {dataset}: {path}"
                ) from exc
            if physical_schema != spec.schema:
                raise DataValidationError(
                    f"schema mismatch for normalized dataset {dataset}: {path}"
                )
            self._validated_files[path] = identity

    @staticmethod
    def _requested_columns(
        spec: DatasetSpec,
        columns: Iterable[str] | None,
    ) -> tuple[str, ...]:
        requested = tuple(columns) if columns is not None else tuple(spec.schema.names)
        if len(requested) != len(set(requested)):
            raise ValueError("scan columns must be unique")
        unknown = set(requested) - set(spec.schema.names)
        if unknown:
            raise ValueError(f"unknown columns for {spec.name}: {sorted(unknown)}")
        return requested

    @staticmethod
    def _projected_schema(spec: DatasetSpec, columns: Iterable[str]) -> pa.Schema:
        return pa.schema([spec.schema.field(column) for column in columns])

    @staticmethod
    def _scan_expression(
        spec: DatasetSpec,
        expression: ds.Expression,
        *,
        equal: dict[str, Any],
        contained_in: dict[str, Iterable[Any]],
        ranges: dict[str, tuple[Any | None, Any | None]],
    ) -> ds.Expression:
        filtered_columns = set(equal) | set(contained_in) | set(ranges)
        unknown = filtered_columns - set(spec.schema.names)
        if unknown:
            raise ValueError(f"unknown filters for {spec.name}: {sorted(unknown)}")
        for column, value in equal.items():
            expression &= ds.field(column) == pa.scalar(value)
        for column, values in contained_in.items():
            accepted = tuple(values)
            if not accepted:
                expression &= ds.scalar(False)
            else:
                expression &= ds.field(column).isin(accepted)
        for column, (lower, upper) in ranges.items():
            if lower is not None:
                expression &= ds.field(column) >= pa.scalar(lower)
            if upper is not None:
                expression &= ds.field(column) <= pa.scalar(upper)
        return expression

    @staticmethod
    def _ensure_unique_sorted_keys(
        table: pa.Table,
        keys: Iterable[str],
        *,
        dataset: str,
    ) -> None:
        if table.num_rows < 2:
            return
        duplicate = None
        for key in keys:
            equal = pc.equal(
                table[key].slice(1),
                table[key].slice(0, table.num_rows - 1),
            )
            duplicate = equal if duplicate is None else pc.and_(duplicate, equal)
        assert duplicate is not None
        if pc.any(duplicate).as_py():
            raise DataValidationError(
                f"duplicate business keys in normalized dataset {dataset}"
            )

    @staticmethod
    def _spec(dataset: str) -> DatasetSpec:
        try:
            return DATASET_SPECS[dataset]
        except KeyError as exc:
            raise ValueError(f"unknown normalized dataset: {dataset}") from exc
