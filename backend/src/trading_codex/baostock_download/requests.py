import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from trading_codex.baostock_download.constants import (
    PROVIDER,
    PROVIDER_CLIENT_VERSION,
)
from trading_codex.baostock_download.endpoints import endpoint
from trading_codex.baostock_download.errors import RequestInputError


def canonical_json(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


@dataclass(frozen=True)
class DownloadRequest:
    operation: str
    provider_method: str
    query: dict[str, str]
    expected_fields: tuple[str, ...]
    request_id: str

    @classmethod
    def from_dict(cls, payload: Any) -> "DownloadRequest":
        if not isinstance(payload, dict):
            raise RequestInputError("each request must be a JSON object")
        if set(payload) != {"operation", "query"}:
            raise RequestInputError("request fields must be exactly operation and query")
        operation = payload.get("operation")
        if not isinstance(operation, str) or not operation:
            raise RequestInputError("request operation must be a non-empty string")
        try:
            contract = endpoint(operation)
            query = contract.validate_query(payload.get("query"))
        except Exception as exc:
            if isinstance(exc, RequestInputError):
                raise
            raise RequestInputError(str(exc)) from exc
        identity = {
            "source": PROVIDER,
            "provider_client_version": PROVIDER_CLIENT_VERSION,
            "operation": operation,
            "query": query,
        }
        request_id = hashlib.sha256(canonical_json(identity)).hexdigest()
        return cls(
            operation=operation,
            provider_method=contract.provider_method,
            query=query,
            expected_fields=contract.expected_fields,
            request_id=request_id,
        )

    @property
    def raw_query(self) -> dict[str, str]:
        return {
            **self.query,
            "_provider_client_version": PROVIDER_CLIENT_VERSION,
        }

    def as_dict(self) -> dict[str, object]:
        return {"operation": self.operation, "query": self.query}

    def raw_path(self, data_root: Path) -> Path:
        return data_root / "raw" / PROVIDER / self.operation / f"{self.request_id}.json"


def read_jsonl(stream: BinaryIO, *, source: str) -> tuple[DownloadRequest, ...]:
    requests = []
    for line_number, raw_line in enumerate(stream, start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RequestInputError(
                f"invalid JSON request at {source}:{line_number}"
            ) from exc
        try:
            requests.append(DownloadRequest.from_dict(payload))
        except RequestInputError as exc:
            raise RequestInputError(f"{source}:{line_number}: {exc}") from exc
    if not requests:
        raise RequestInputError(f"request stream is empty: {source}")
    return tuple(requests)
