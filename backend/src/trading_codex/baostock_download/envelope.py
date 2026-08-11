import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_codex.baostock_download.constants import PROVIDER_CLIENT_VERSION
from trading_codex.baostock_download.endpoints import endpoint
from trading_codex.baostock_download.requests import DownloadRequest, canonical_json
from trading_codex.data.models import ProviderBatch, RawArtifact, RawIntegrityError

ENVELOPE_V1_FIELDS = {
    "schema_version",
    "source",
    "operation",
    "query",
    "fields",
    "rows",
    "content_sha256",
    "received_at",
}
ENVELOPE_V2_FIELDS = ENVELOPE_V1_FIELDS | {"envelope_sha256"}


@dataclass(frozen=True)
class VerifiedEnvelope:
    batch: ProviderBatch
    artifact: RawArtifact


def encode_envelope(batch: ProviderBatch) -> tuple[bytes, str]:
    content = {
        "schema_version": 2,
        "source": batch.source,
        "operation": batch.operation,
        "query": batch.query,
        "fields": list(batch.fields),
        "rows": list(batch.rows),
    }
    content_sha256 = hashlib.sha256(canonical_json(content)).hexdigest()
    body = {
        **content,
        "content_sha256": content_sha256,
        "received_at": batch.received_at.astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    envelope = {
        **body,
        "envelope_sha256": hashlib.sha256(canonical_json(body)).hexdigest(),
    }
    return canonical_json(envelope), content_sha256


def verify_envelope(path: Path, *, raw_root: Path) -> VerifiedEnvelope:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RawIntegrityError(f"cannot read raw envelope: {path}") from exc
    return verify_envelope_bytes(payload, path=path, raw_root=raw_root)


def verify_envelope_bytes(
    payload: bytes,
    *,
    path: Path,
    raw_root: Path,
) -> VerifiedEnvelope:
    try:
        envelope = json.loads(payload)
        if not isinstance(envelope, dict):
            raise TypeError("envelope must be an object")
        if payload != canonical_json(envelope):
            raise TypeError("envelope must be canonical JSON")
        schema_version = envelope["schema_version"]
        if schema_version not in {1, 2} or isinstance(schema_version, bool):
            raise TypeError("unsupported envelope schema version")
        expected_fields = (
            ENVELOPE_V1_FIELDS if schema_version == 1 else ENVELOPE_V2_FIELDS
        )
        if set(envelope) != expected_fields:
            raise TypeError(f"envelope fields differ from schema version {schema_version}")
        source = envelope["source"]
        operation = envelope["operation"]
        query = envelope["query"]
        fields = envelope["fields"]
        rows = envelope["rows"]
        recorded_sha256 = envelope["content_sha256"]
        envelope_sha256 = envelope.get("envelope_sha256")
        received_at = datetime.fromisoformat(
            envelope["received_at"].replace("Z", "+00:00")
        )
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise TypeError("envelope received_at must include a UTC offset")
        received_at = received_at.astimezone(UTC)
        if source != "baostock" or not isinstance(operation, str):
            raise TypeError("envelope provider identity is invalid")
        if not isinstance(query, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in query.items()
        ):
            raise TypeError("envelope query must contain string keys and values")
        if not isinstance(fields, list) or any(
            not isinstance(field, str) for field in fields
        ):
            raise TypeError("envelope fields must be strings")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise TypeError("envelope rows must be objects")
        if not isinstance(recorded_sha256, str) or len(recorded_sha256) != 64:
            raise TypeError("envelope content SHA-256 is invalid")
        if schema_version == 2 and (
            not isinstance(envelope_sha256, str) or len(envelope_sha256) != 64
        ):
            raise TypeError("envelope SHA-256 is invalid")
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise RawIntegrityError(f"invalid raw envelope: {path}") from exc

    semantic_query = dict(query)
    provider_version = semantic_query.pop("_provider_client_version", None)
    if schema_version == 2 and provider_version != PROVIDER_CLIENT_VERSION:
        raise RawIntegrityError(f"missing or unsupported BaoStock client version in {path}")
    if schema_version == 1 and provider_version not in {
        None,
        PROVIDER_CLIENT_VERSION,
    }:
        raise RawIntegrityError(f"unsupported BaoStock client version in {path}")
    try:
        contract = endpoint(operation)
        semantic_query = contract.validate_query(semantic_query)
    except Exception as exc:
        raise RawIntegrityError(f"raw envelope query contract failed: {path}") from exc
    if tuple(fields) != contract.expected_fields:
        raise RawIntegrityError(f"raw envelope field contract failed: {path}")

    content = {
        "schema_version": schema_version,
        "source": source,
        "operation": operation,
        "query": query,
        "fields": fields,
        "rows": rows,
    }
    actual_sha256 = hashlib.sha256(canonical_json(content)).hexdigest()
    if actual_sha256 != recorded_sha256:
        raise RawIntegrityError(f"raw envelope content hash mismatch: {path}")
    if schema_version == 2:
        body = {key: value for key, value in envelope.items() if key != "envelope_sha256"}
        actual_envelope_sha256 = hashlib.sha256(canonical_json(body)).hexdigest()
        if actual_envelope_sha256 != envelope_sha256:
            raise RawIntegrityError(f"raw envelope hash mismatch: {path}")
    try:
        ordered_rows = tuple(
            {field: row[field] for field in fields}
            for row in rows
        )
        if any(
            set(row) != set(fields)
            or any(not isinstance(value, str) for value in row.values())
            for row in rows
        ):
            raise TypeError("row fields or values differ from the provider contract")
        batch = ProviderBatch(
            source=source,
            operation=operation,
            query=dict(query),
            fields=tuple(fields),
            rows=ordered_rows,
            received_at=received_at,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RawIntegrityError(f"cannot reconstruct raw envelope: {path}") from exc

    try:
        relative = path.resolve().relative_to(raw_root.resolve())
    except ValueError as exc:
        raise RawIntegrityError(f"raw envelope is outside its raw root: {path}") from exc
    if (
        len(relative.parts) != 3
        or relative.parts[0] != source
        or relative.parts[1] != operation
        or path.suffix != ".json"
    ):
        raise RawIntegrityError(f"raw envelope path does not match its identity: {path}")

    expected_request_id = DownloadRequest.from_dict(
        {"operation": operation, "query": semantic_query}
    ).request_id
    expected_stems = (
        {expected_request_id}
        if schema_version == 2
        else {recorded_sha256, expected_request_id}
    )
    if path.stem not in expected_stems:
        raise RawIntegrityError(f"raw envelope filename does not match its address: {path}")
    return VerifiedEnvelope(
        batch=batch,
        artifact=RawArtifact(
            path=path,
            relative_path=relative.as_posix(),
            content_sha256=recorded_sha256,
            received_at=received_at,
        ),
    )
