import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_codex.baostock_download.constants import (
    BLACKLIST_RULES_SHA256,
    DEFAULT_CALENDAR_DAY_LIMIT,
    DEFAULT_ROLLING_24H_LIMIT,
    DEFAULT_SESSION_ATTEMPT_LIMIT,
    MINIMUM_INTERVAL_SECONDS,
    PROJECT_CALENDAR_DAY_HARD_LIMIT,
    PROVIDER,
    PROVIDER_CLIENT_VERSION,
)
from trading_codex.baostock_download.endpoints import endpoint
from trading_codex.baostock_download.errors import ManifestError

BOUNDARY_NAMES = ("warmup", "train", "validation", "test")


class _DuplicateJsonKey(ValueError):
    pass


def canonical_json(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def strict_json_loads(payload: bytes) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(
        payload,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RequestLimits:
    calendar_day_attempts: int = DEFAULT_CALENDAR_DAY_LIMIT
    rolling_24h_attempts: int = DEFAULT_ROLLING_24H_LIMIT
    session_attempts: int = DEFAULT_SESSION_ATTEMPT_LIMIT
    minimum_interval_seconds: float = MINIMUM_INTERVAL_SECONDS

    @classmethod
    def from_dict(cls, payload: Any) -> "RequestLimits":
        if payload is None:
            result = cls()
        elif isinstance(payload, dict):
            allowed = {
                "calendar_day_attempts",
                "rolling_24h_attempts",
                "session_attempts",
                "minimum_interval_seconds",
            }
            extra = sorted(set(payload) - allowed)
            if extra:
                raise ManifestError(f"unsupported limit fields: {', '.join(extra)}")
            interval = payload.get(
                "minimum_interval_seconds", MINIMUM_INTERVAL_SECONDS
            )
            if isinstance(interval, bool) or not isinstance(interval, (int, float)):
                raise ManifestError("minimum_interval_seconds must be numeric")
            result = cls(
                calendar_day_attempts=_limit_integer(
                    payload,
                    "calendar_day_attempts",
                    DEFAULT_CALENDAR_DAY_LIMIT,
                ),
                rolling_24h_attempts=_limit_integer(
                    payload,
                    "rolling_24h_attempts",
                    DEFAULT_ROLLING_24H_LIMIT,
                ),
                session_attempts=_limit_integer(
                    payload,
                    "session_attempts",
                    DEFAULT_SESSION_ATTEMPT_LIMIT,
                ),
                minimum_interval_seconds=float(interval),
            )
        else:
            raise ManifestError("limits must be an object")
        result.validate()
        return result

    def validate(self) -> None:
        for name in (
            "calendar_day_attempts",
            "rolling_24h_attempts",
            "session_attempts",
        ):
            value = getattr(self, name)
            if value < 1:
                raise ManifestError(f"{name} must be positive")
            if value > PROJECT_CALENDAR_DAY_HARD_LIMIT:
                raise ManifestError(
                    f"{name} exceeds the project hard limit of "
                    f"{PROJECT_CALENDAR_DAY_HARD_LIMIT}"
                )
        if not math.isfinite(self.minimum_interval_seconds):
            raise ManifestError("minimum_interval_seconds must be finite")
        if self.minimum_interval_seconds < MINIMUM_INTERVAL_SECONDS:
            raise ManifestError(
                "minimum_interval_seconds cannot be lower than "
                f"{MINIMUM_INTERVAL_SECONDS:g}"
            )

    def as_dict(self) -> dict[str, int | float]:
        return {
            "calendar_day_attempts": self.calendar_day_attempts,
            "rolling_24h_attempts": self.rolling_24h_attempts,
            "session_attempts": self.session_attempts,
            "minimum_interval_seconds": self.minimum_interval_seconds,
        }


@dataclass(frozen=True)
class ManifestItem:
    item_id: str
    operation: str
    endpoint: str
    query: dict[str, str]
    expected_fields: tuple[str, ...]
    max_pages: int
    max_attempts: int
    dependencies: tuple[str, ...]

    @property
    def raw_query(self) -> dict[str, str]:
        return {
            **self.query,
            "_provider_client_version": PROVIDER_CLIENT_VERSION,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "operation": self.operation,
            "endpoint": self.endpoint,
            "query": self.query,
            "expected_fields": list(self.expected_fields),
            "max_pages": self.max_pages,
            "max_attempts": self.max_attempts,
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    manifest_id: str
    manifest_sha256: str
    status: str
    provider: str
    provider_client_version: str
    provider_rules_sha256: str
    created_at: str
    created_by: str
    boundaries: dict[str, str | None]
    limits: RequestLimits
    estimated_peak_bytes: int
    items: tuple[ManifestItem, ...]

    @classmethod
    def from_dict(cls, payload: Any) -> "Manifest":
        if not isinstance(payload, dict):
            raise ManifestError("manifest must be a JSON object")
        allowed = {
            "schema_version",
            "manifest_id",
            "manifest_sha256",
            "status",
            "provider",
            "provider_client_version",
            "provider_rules_sha256",
            "created_at",
            "created_by",
            "boundaries",
            "limits",
            "estimated_peak_bytes",
            "items",
        }
        extra = sorted(set(payload) - allowed)
        if extra:
            raise ManifestError(f"unsupported manifest fields: {', '.join(extra)}")
        try:
            schema_version = payload["schema_version"]
            status = payload["status"]
            provider = payload["provider"]
            provider_version = payload["provider_client_version"]
            provider_rules_sha256 = payload["provider_rules_sha256"]
            created_at = payload["created_at"]
            created_by = payload["created_by"]
            manifest_id = payload["manifest_id"]
            recorded_sha = payload["manifest_sha256"]
            estimated_peak_bytes = payload["estimated_peak_bytes"]
        except KeyError as exc:
            raise ManifestError(f"manifest is missing {exc.args[0]}") from exc
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != 1
        ):
            raise ManifestError("unsupported manifest schema_version")
        if not isinstance(status, str) or status not in {"draft", "frozen"}:
            raise ManifestError("manifest status must be draft or frozen")
        if (
            provider != PROVIDER
            or provider_version != PROVIDER_CLIENT_VERSION
            or provider_rules_sha256 != BLACKLIST_RULES_SHA256
        ):
            raise ManifestError("manifest provider identity does not match this downloader")
        _parse_timestamp(created_at, field="created_at")
        if not isinstance(created_by, str) or not created_by.strip():
            raise ManifestError("created_by must be a non-empty string")
        boundaries = _boundaries(payload.get("boundaries"))
        limits = RequestLimits.from_dict(payload.get("limits"))
        if (
            not isinstance(estimated_peak_bytes, int)
            or isinstance(estimated_peak_bytes, bool)
            or estimated_peak_bytes < 0
        ):
            raise ManifestError("estimated_peak_bytes must be a non-negative integer")
        items_payload = payload.get("items")
        if not isinstance(items_payload, list) or not items_payload:
            raise ManifestError("manifest must contain at least one item")
        items = tuple(_load_item(item) for item in items_payload)
        _validate_dependencies(items)
        result = cls(
            schema_version=1,
            manifest_id=manifest_id,
            manifest_sha256=recorded_sha,
            status=status,
            provider=provider,
            provider_client_version=provider_version,
            provider_rules_sha256=provider_rules_sha256,
            created_at=created_at,
            created_by=created_by,
            boundaries=boundaries,
            limits=limits,
            estimated_peak_bytes=estimated_peak_bytes,
            items=items,
        )
        expected_sha = _sha256(result.semantic_dict())
        expected_id = f"bs-{expected_sha[:20]}"
        if recorded_sha != expected_sha or manifest_id != expected_id:
            raise ManifestError("manifest identity or SHA-256 does not match its contents")
        return result

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "provider_client_version": self.provider_client_version,
            "provider_rules_sha256": self.provider_rules_sha256,
            "boundaries": self.boundaries,
            "limits": self.limits.as_dict(),
            "estimated_peak_bytes": self.estimated_peak_bytes,
            "items": [item.as_dict() for item in self.items],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_dict(),
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "status": self.status,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    def frozen(self) -> "Manifest":
        return Manifest(
            **{
                **self.__dict__,
                "status": "frozen",
            }
        )


def create_manifest(
    spec: Any,
    *,
    created_at: datetime,
) -> Manifest:
    if not isinstance(spec, dict):
        raise ManifestError("plan spec must be a JSON object")
    allowed = {"created_by", "boundaries", "limits", "estimated_peak_bytes", "items"}
    extra = sorted(set(spec) - allowed)
    if extra:
        raise ManifestError(f"unsupported plan spec fields: {', '.join(extra)}")
    created_by = spec.get("created_by")
    if not isinstance(created_by, str) or not created_by.strip():
        raise ManifestError("plan spec created_by must be a non-empty string")
    boundaries = _boundaries(spec.get("boundaries"))
    limits = RequestLimits.from_dict(spec.get("limits"))
    estimated_peak_bytes = spec.get("estimated_peak_bytes", 0)
    if (
        not isinstance(estimated_peak_bytes, int)
        or isinstance(estimated_peak_bytes, bool)
        or estimated_peak_bytes < 0
    ):
        raise ManifestError("estimated_peak_bytes must be a non-negative integer")
    raw_items = spec.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ManifestError("plan spec must contain at least one item")

    aliases: dict[str, str] = {}
    items: list[ManifestItem] = []
    pending_dependencies: list[tuple[str, ...]] = []
    for index, raw_item in enumerate(raw_items, start=1):
        item, alias, dependencies = _create_item(raw_item, index=index)
        if alias in aliases:
            raise ManifestError(f"duplicate plan item key: {alias}")
        aliases[alias] = item.item_id
        items.append(item)
        pending_dependencies.append(dependencies)

    resolved_items = []
    seen: set[str] = set()
    for item, dependency_names in zip(items, pending_dependencies, strict=True):
        dependencies = []
        for dependency in dependency_names:
            dependency_id = aliases.get(dependency, dependency)
            if dependency_id not in seen:
                raise ManifestError(
                    f"item {item.item_id} dependency must reference an earlier item: {dependency}"
                )
            dependencies.append(dependency_id)
        resolved_items.append(
            ManifestItem(
                **{
                    **item.__dict__,
                    "dependencies": tuple(dependencies),
                }
            )
        )
        seen.add(item.item_id)

    semantic = {
        "schema_version": 1,
        "provider": PROVIDER,
        "provider_client_version": PROVIDER_CLIENT_VERSION,
        "provider_rules_sha256": BLACKLIST_RULES_SHA256,
        "boundaries": boundaries,
        "limits": limits.as_dict(),
        "estimated_peak_bytes": estimated_peak_bytes,
        "items": [item.as_dict() for item in resolved_items],
    }
    manifest_sha256 = _sha256(semantic)
    return Manifest(
        schema_version=1,
        manifest_id=f"bs-{manifest_sha256[:20]}",
        manifest_sha256=manifest_sha256,
        status="draft",
        provider=PROVIDER,
        provider_client_version=PROVIDER_CLIENT_VERSION,
        provider_rules_sha256=BLACKLIST_RULES_SHA256,
        created_at=_utc_text(created_at),
        created_by=created_by.strip(),
        boundaries=boundaries,
        limits=limits,
        estimated_peak_bytes=estimated_peak_bytes,
        items=tuple(resolved_items),
    )


def load_manifest(path: Path) -> Manifest:
    try:
        raw = path.read_bytes()
        payload = strict_json_loads(raw)
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest does not exist: {path}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {path}") from exc
    manifest = Manifest.from_dict(payload)
    if raw != canonical_json(manifest.as_dict()):
        raise ManifestError(f"manifest is not canonical JSON: {path}")
    return manifest


def write_draft(data_root: Path, manifest: Manifest) -> Path:
    if manifest.status != "draft":
        raise ManifestError("only draft manifests can be written to the draft directory")
    path = data_root / "manifests" / "draft" / f"{manifest.manifest_id}.json"
    _write_manifest(path, manifest)
    return path


def freeze_manifest(data_root: Path, draft_path: Path) -> tuple[Manifest, Path]:
    manifest = load_manifest(draft_path)
    if manifest.status != "draft":
        raise ManifestError("plan freeze accepts only a draft manifest")
    expected_parent = (data_root / "manifests" / "draft").resolve()
    if draft_path.resolve().parent != expected_parent:
        raise ManifestError("draft manifest must be inside this data root")
    frozen = manifest.frozen()
    path = data_root / "manifests" / "frozen" / f"{frozen.manifest_id}.json"
    _write_manifest(path, frozen)
    return frozen, path


def require_frozen(data_root: Path, path: Path) -> Manifest:
    expected_parent = (data_root / "manifests" / "frozen").resolve()
    if path.resolve().parent != expected_parent:
        raise ManifestError("frozen manifest must be inside this data root")
    manifest = load_manifest(path)
    if manifest.status != "frozen":
        raise ManifestError("fetch, sync, status, and verify require a frozen manifest")
    if path.name != f"{manifest.manifest_id}.json":
        raise ManifestError("frozen manifest filename does not match manifest_id")
    return manifest


def _create_item(
    payload: Any,
    *,
    index: int,
) -> tuple[ManifestItem, str, tuple[str, ...]]:
    if not isinstance(payload, dict):
        raise ManifestError("plan items must be objects")
    allowed = {
        "key",
        "operation",
        "query",
        "expected_fields",
        "max_pages",
        "max_attempts",
        "dependencies",
    }
    extra = sorted(set(payload) - allowed)
    if extra:
        raise ManifestError(f"unsupported plan item fields: {', '.join(extra)}")
    operation = payload.get("operation")
    if not isinstance(operation, str):
        raise ManifestError("plan item operation must be a string")
    contract = endpoint(operation)
    query = contract.validate_query(payload.get("query"))
    supplied_fields = payload.get("expected_fields", list(contract.expected_fields))
    if supplied_fields != list(contract.expected_fields):
        raise ManifestError(f"{operation} expected_fields must match the fixed endpoint contract")
    max_pages = _positive_integer(payload.get("max_pages", 1), field="max_pages")
    max_attempts = _positive_integer(
        payload.get("max_attempts", max_pages), field="max_attempts"
    )
    if max_attempts < max_pages:
        raise ManifestError("max_attempts cannot be lower than max_pages")
    if not contract.paginated and max_pages != 1:
        raise ManifestError(f"{operation} is a single-page endpoint")
    if max_attempts > DEFAULT_SESSION_ATTEMPT_LIMIT:
        raise ManifestError("item page budget exceeds the default session hard boundary")
    identity = {
        "provider": PROVIDER,
        "provider_client_version": PROVIDER_CLIENT_VERSION,
        "operation": operation,
        "endpoint": contract.provider_method,
        "query": query,
        "expected_fields": list(contract.expected_fields),
    }
    item_id = f"item-{_sha256(identity)[:20]}"
    alias = payload.get("key", f"item-{index:04d}")
    if not isinstance(alias, str) or not alias:
        raise ManifestError("plan item key must be a non-empty string")
    dependencies = payload.get("dependencies", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(value, str) or not value for value in dependencies
    ):
        raise ManifestError("item dependencies must be a list of item keys")
    return (
        ManifestItem(
            item_id=item_id,
            operation=operation,
            endpoint=contract.provider_method,
            query=query,
            expected_fields=contract.expected_fields,
            max_pages=max_pages,
            max_attempts=max_attempts,
            dependencies=(),
        ),
        alias,
        tuple(dependencies),
    )


def _load_item(payload: Any) -> ManifestItem:
    if not isinstance(payload, dict):
        raise ManifestError("manifest items must be objects")
    allowed = {
        "item_id",
        "operation",
        "endpoint",
        "query",
        "expected_fields",
        "max_pages",
        "max_attempts",
        "dependencies",
    }
    extra = sorted(set(payload) - allowed)
    if extra:
        raise ManifestError(f"unsupported manifest item fields: {', '.join(extra)}")
    try:
        operation = payload["operation"]
        recorded_id = payload["item_id"]
        recorded_endpoint = payload["endpoint"]
        fields = payload["expected_fields"]
        max_pages = payload["max_pages"]
        max_attempts = payload["max_attempts"]
        dependencies = payload["dependencies"]
    except KeyError as exc:
        raise ManifestError(f"manifest item is missing {exc.args[0]}") from exc
    created, _, _ = _create_item(
        {
            "operation": operation,
            "query": payload.get("query"),
            "expected_fields": fields,
            "max_pages": max_pages,
            "max_attempts": max_attempts,
        },
        index=1,
    )
    if recorded_id != created.item_id or recorded_endpoint != created.endpoint:
        raise ManifestError("manifest item identity does not match its endpoint contract")
    if not isinstance(dependencies, list) or any(
        not isinstance(value, str) for value in dependencies
    ):
        raise ManifestError("manifest item dependencies must be strings")
    return ManifestItem(**{**created.__dict__, "dependencies": tuple(dependencies)})


def _validate_dependencies(items: tuple[ManifestItem, ...]) -> None:
    seen: set[str] = set()
    for item in items:
        if item.item_id in seen:
            raise ManifestError(f"manifest contains duplicate item: {item.item_id}")
        if any(dependency not in seen for dependency in item.dependencies):
            raise ManifestError(f"item {item.item_id} has a missing or forward dependency")
        seen.add(item.item_id)


def _boundaries(payload: Any) -> dict[str, str | None]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ManifestError("boundaries must be an object")
    extra = sorted(set(payload) - set(BOUNDARY_NAMES))
    if extra:
        raise ManifestError(f"unsupported boundaries: {', '.join(extra)}")
    boundaries = {name: payload.get(name) for name in BOUNDARY_NAMES}
    for name, value in boundaries.items():
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"boundary {name} must be a string or null")
    return boundaries


def _positive_integer(value: Any, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ManifestError(f"{field} must be a positive integer")
    return value


def _limit_integer(payload: dict[str, Any], field: str, default: int) -> int:
    value = payload.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestError(f"{field} must be an integer")
    return value


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ManifestError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestError(f"{field} must include a UTC offset")
    return parsed


def durable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _write_manifest(path: Path, manifest: Manifest) -> None:
    payload = canonical_json(manifest.as_dict())
    if path.exists():
        existing = load_manifest(path)
        if (
            existing.status == manifest.status
            and existing.manifest_sha256 == manifest.manifest_sha256
        ):
            return
        raise ManifestError(f"refusing to replace an existing manifest: {path}")
    durable_write(path, payload)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
