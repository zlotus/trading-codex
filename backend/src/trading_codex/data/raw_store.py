import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_codex.data.models import ProviderBatch, RawArtifact, RawIntegrityError


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class ImmutableRawStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def persist(self, batch: ProviderBatch) -> RawArtifact:
        content = {
            "schema_version": 1,
            "source": batch.source,
            "operation": batch.operation,
            "query": batch.query,
            "fields": list(batch.fields),
            "rows": list(batch.rows),
        }
        content_sha256 = hashlib.sha256(_canonical_json(content)).hexdigest()
        path = self.root / batch.source / batch.operation / f"{content_sha256}.json"

        if path.exists():
            artifact = self._read_verified(path, content_sha256)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            envelope = {
                **content,
                "content_sha256": content_sha256,
                "received_at": batch.received_at.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            payload = _canonical_json(envelope)
            try:
                with path.open("xb") as artifact_file:
                    artifact_file.write(payload)
                    artifact_file.flush()
                    os.fsync(artifact_file.fileno())
            except FileExistsError:
                artifact = self._read_verified(path, content_sha256)
            else:
                _fsync_directory(path.parent)
                artifact = RawArtifact(
                    path=path,
                    relative_path=path.relative_to(self.root).as_posix(),
                    content_sha256=content_sha256,
                    received_at=batch.received_at.astimezone(UTC),
                )

        self._write_query_index(batch, artifact)
        return artifact

    def lookup(
        self,
        *,
        source: str,
        operation: str,
        query: dict[str, str],
    ) -> ProviderBatch | None:
        cached = self.lookup_with_artifact(
            source=source,
            operation=operation,
            query=query,
        )
        return cached[0] if cached is not None else None

    def lookup_with_artifact(
        self,
        *,
        source: str,
        operation: str,
        query: dict[str, str],
    ) -> tuple[ProviderBatch, RawArtifact] | None:
        key = self.query_key(source=source, operation=operation, query=query)
        index_path = self._index_path(source, operation, key)
        if index_path.exists():
            try:
                raw_index = index_path.read_bytes()
                index = json.loads(raw_index)
                if not isinstance(index, dict) or raw_index != _canonical_json(index):
                    raise TypeError("raw query index must be a canonical object")
                if set(index) != {
                    "schema_version",
                    "source",
                    "operation",
                    "query",
                    "content_sha256",
                    "raw_artifact",
                }:
                    raise TypeError("raw query index fields differ from the contract")
                if (
                    index.get("schema_version") != 1
                    or isinstance(index.get("schema_version"), bool)
                    or index.get("source") != source
                    or index.get("operation") != operation
                    or index.get("query") != query
                ):
                    raise TypeError("raw query index identity differs from the contract")
                relative_path = index["raw_artifact"]
                expected_sha256 = index["content_sha256"]
                if (
                    not isinstance(relative_path, str)
                    or not isinstance(expected_sha256, str)
                    or len(expected_sha256) != 64
                ):
                    raise TypeError("raw query index address is invalid")
            except (
                KeyError,
                TypeError,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ) as exc:
                raise RawIntegrityError(f"invalid raw query index: {index_path}") from exc
            path = self._resolve_artifact(relative_path)
            envelope, artifact = self._load_verified(path, expected_sha256)
            return self._batch_from_envelope(envelope, source, operation, query), artifact

        cached = self._scan_for_query(source=source, operation=operation, query=query)
        if cached is None:
            return None
        batch, artifact = cached
        self._write_query_index(batch, artifact)
        return batch, artifact

    def iter_verified(self, *, source: str) -> list[tuple[ProviderBatch, RawArtifact]]:
        directory = self.root / source
        if not directory.exists():
            return []
        verified = []
        for path in sorted(directory.glob("*/*.json")):
            operation = path.parent.name
            envelope, artifact = self._load_verified(path, path.stem)
            query = envelope.get("query")
            if not isinstance(query, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in query.items()
            ):
                raise RawIntegrityError(f"invalid raw artifact query: {path}")
            verified.append(
                (
                    self._batch_from_envelope(envelope, source, operation, query),
                    artifact,
                )
            )
        return verified

    @staticmethod
    def query_key(*, source: str, operation: str, query: dict[str, str]) -> str:
        payload = {"source": source, "operation": operation, "query": query}
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def _read_verified(self, path: Path, expected_sha256: str) -> RawArtifact:
        _, artifact = self._load_verified(path, expected_sha256)
        return artifact

    def _load_verified(
        self, path: Path, expected_sha256: str
    ) -> tuple[dict[str, Any], RawArtifact]:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RawIntegrityError(f"cannot read raw artifact: {path}") from exc
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise TypeError("raw artifact envelope must be an object")
            if raw != _canonical_json(envelope):
                raise TypeError("raw artifact envelope must be canonical JSON")
            if set(envelope) != {
                "schema_version",
                "source",
                "operation",
                "query",
                "fields",
                "rows",
                "content_sha256",
                "received_at",
            }:
                raise TypeError("raw artifact fields differ from the contract")
            if envelope.get("schema_version") != 1 or isinstance(
                envelope.get("schema_version"), bool
            ):
                raise TypeError("raw artifact schema version is invalid")
            recorded_sha256 = envelope.pop("content_sha256")
            received_at = envelope.pop("received_at")
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RawIntegrityError(f"invalid raw artifact: {path}") from exc

        actual_sha256 = hashlib.sha256(_canonical_json(envelope)).hexdigest()
        if recorded_sha256 != expected_sha256 or actual_sha256 != expected_sha256:
            raise RawIntegrityError(f"raw artifact content hash mismatch: {path}")

        try:
            parsed_received_at = _parse_timestamp(received_at)
        except (TypeError, ValueError) as exc:
            raise RawIntegrityError(f"invalid raw artifact timestamp: {path}") from exc
        artifact = RawArtifact(
            path=path,
            relative_path=path.relative_to(self.root).as_posix(),
            content_sha256=expected_sha256,
            received_at=parsed_received_at,
        )
        envelope["content_sha256"] = expected_sha256
        envelope["received_at"] = received_at
        return envelope, artifact

    def _scan_for_query(
        self, *, source: str, operation: str, query: dict[str, str]
    ) -> tuple[ProviderBatch, RawArtifact] | None:
        directory = self.root / source / operation
        if not directory.exists():
            return None
        matches: list[tuple[ProviderBatch, RawArtifact]] = []
        for path in directory.glob("*.json"):
            envelope, artifact = self._load_verified(path, path.stem)
            if envelope.get("query") == query:
                matches.append(
                    (
                        self._batch_from_envelope(envelope, source, operation, query),
                        artifact,
                    )
                )
        return max(matches, key=lambda item: item[0].received_at, default=None)

    @staticmethod
    def _batch_from_envelope(
        envelope: dict[str, Any], source: str, operation: str, query: dict[str, str]
    ) -> ProviderBatch:
        if envelope.get("source") != source or envelope.get("operation") != operation:
            raise RawIntegrityError("raw artifact identity does not match its query index")
        if envelope.get("query") != query:
            raise RawIntegrityError("raw artifact query does not match its query index")
        fields = tuple(envelope.get("fields", ()))
        raw_rows = envelope.get("rows")
        if not fields or not isinstance(raw_rows, list):
            raise RawIntegrityError("raw artifact is missing fields or rows")
        try:
            rows = tuple(dict(row) for row in raw_rows)
            received_at = _parse_timestamp(envelope["received_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RawIntegrityError("raw artifact cannot be reconstructed") from exc
        return ProviderBatch(
            source=source,
            operation=operation,
            query=query,
            fields=fields,
            rows=rows,
            received_at=received_at,
        )

    def _write_query_index(self, batch: ProviderBatch, artifact: RawArtifact) -> None:
        key = self.query_key(
            source=batch.source, operation=batch.operation, query=batch.query
        )
        path = self._index_path(batch.source, batch.operation, key)
        payload = _canonical_json(
            {
                "schema_version": 1,
                "source": batch.source,
                "operation": batch.operation,
                "query": batch.query,
                "content_sha256": artifact.content_sha256,
                "raw_artifact": artifact.relative_path,
            }
        )
        if path.exists() and path.read_bytes() == payload:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".tmp", delete=False
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            _fsync_directory(path.parent)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def _index_path(self, source: str, operation: str, key: str) -> Path:
        return self.root / ".query-cache" / source / operation / f"{key}.json"

    def _resolve_artifact(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise RawIntegrityError("raw query index points outside the raw store")
        return path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
