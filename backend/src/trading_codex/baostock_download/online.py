import importlib
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from types import ModuleType
from typing import Any

from trading_codex.baostock_download.constants import (
    BLACKLIST_ERROR_CODE,
    DEFAULT_MAX_ITEMS,
    MAX_SESSION_ITEMS,
    PROVIDER_CLIENT_VERSION,
)
from trading_codex.baostock_download.errors import (
    BudgetExceeded,
    ManifestError,
    ProviderBlacklisted,
    ProviderFailure,
    SchemaDriftError,
    StateError,
)
from trading_codex.baostock_download.manifest import (
    Manifest,
    ManifestItem,
    canonical_json,
    durable_write,
)
from trading_codex.baostock_download.offline import data_root_lock
from trading_codex.baostock_download.provider import BaoStockProvider
from trading_codex.baostock_download.state import GlobalProviderLock, StateStore
from trading_codex.baostock_download.storage import StorageGuard, StorageReport
from trading_codex.data.models import ProviderBatch
from trading_codex.data.raw_store import ImmutableRawStore


class SocketAttemptGate:
    def __init__(
        self,
        *,
        state: StateStore,
        session_id: str,
        manifest: Manifest,
        socket_module: ModuleType,
        protocol_constants: ModuleType,
    ) -> None:
        self.state = state
        self.session_id = session_id
        self.manifest = manifest
        self.socket_module = socket_module
        self.protocol_constants = protocol_constants
        self._original_send = socket_module.send_msg
        self._phase = "unbound"
        self._item: ManifestItem | None = None
        self._item_sends = 0
        self.transport_failed = False
        self.blacklist_incident_id: str | None = None
        self.last_attempt_id: str | None = None

    @contextmanager
    def installed(self):
        self.socket_module.send_msg = self.send_msg
        try:
            yield self
        finally:
            self.socket_module.send_msg = self._original_send

    def bind_login(self) -> None:
        self._phase = "login"
        self._item = None
        self._item_sends = 0

    def bind_item(self, item: ManifestItem) -> None:
        self._phase = "item"
        self._item = item
        self._item_sends = 0

    def bind_logout(self) -> None:
        self._phase = "logout"
        self._item = None
        self._item_sends = 0

    def send_msg(self, message: str) -> Any:
        item = self._item
        if self._phase == "unbound":
            raise StateError("BaoStock socket send occurred outside an approved phase")
        if item is not None and self._item_sends >= item.max_pages:
            raise BudgetExceeded(f"item page limit exhausted before send for {item.item_id}")
        kind = self._kind()
        self.state.wait_for_cooldown(self.manifest.limits)
        attempt_id = self.state.reserve_attempt(
            session_id=self.session_id,
            manifest_sha256=self.manifest.manifest_sha256,
            item_id=item.item_id if item is not None else None,
            kind=kind,
            limits=self.manifest.limits,
            item_attempt_limit=item.max_attempts if item is not None else None,
        )
        self.last_attempt_id = attempt_id
        if item is not None:
            self._item_sends += 1
        try:
            response = self._original_send(message)
        except Exception as exc:
            self.transport_failed = True
            self.state.record_attempt_result(
                attempt_id,
                status="transport_error",
                message=f"{type(exc).__name__}: {exc}",
            )
            raise

        code = self._response_code(response)
        if code == BLACKLIST_ERROR_CODE:
            self.state.record_attempt_result(
                attempt_id,
                status="provider_blacklisted",
                provider_code=code,
                message="BaoStock provider blacklist response",
            )
            self.blacklist_incident_id = self.state.record_blacklist(
                session_id=self.session_id,
                attempt_id=attempt_id,
                detail={"provider_code": code, "phase": self._phase, "kind": kind},
            )
            raise ProviderBlacklisted(
                f"BaoStock returned persistent blacklist error {BLACKLIST_ERROR_CODE}"
            )
        self.state.record_attempt_result(
            attempt_id,
            status="succeeded" if code == "0" else "provider_error",
            provider_code=code,
            message=None if code is not None else "response code could not be decoded",
        )
        return response

    def _kind(self) -> str:
        if self._phase != "item":
            return self._phase
        return "query" if self._item_sends == 0 else "page"

    def _response_code(self, response: Any) -> str | None:
        if not isinstance(response, str):
            return None
        header_length = getattr(self.protocol_constants, "MESSAGE_HEADER_LENGTH", None)
        separator = getattr(self.protocol_constants, "MESSAGE_SPLIT", None)
        if not isinstance(header_length, int) or not isinstance(separator, str):
            return None
        body = response[header_length:]
        code, found, _ = body.partition(separator)
        return code if found else None


def fetch_manifest(
    *,
    data_root: Path,
    manifest: Manifest,
    confirmed_sha256: str,
    max_items: int = DEFAULT_MAX_ITEMS,
    state: StateStore,
    storage: StorageGuard,
    provider_module: Any | None = None,
    socket_module: ModuleType | None = None,
    protocol_constants: ModuleType | None = None,
) -> dict[str, object]:
    if manifest.status != "frozen":
        raise ManifestError("fetch accepts only frozen manifests")
    if confirmed_sha256 != manifest.manifest_sha256:
        raise ManifestError("--confirm-manifest-sha256 does not match the frozen manifest")
    if max_items < 1 or max_items > MAX_SESSION_ITEMS:
        raise ManifestError(f"--max-items must be between 1 and {MAX_SESSION_ITEMS}")
    storage_report = storage.preflight(
        initialize=False,
        estimated_peak_bytes=manifest.estimated_peak_bytes,
    )
    with data_root_lock(data_root):
        return _fetch_manifest_locked(
            data_root=data_root,
            manifest=manifest,
            max_items=max_items,
            state=state,
            storage_report=storage_report,
            provider_module=provider_module,
            socket_module=socket_module,
            protocol_constants=protocol_constants,
        )


def _fetch_manifest_locked(
    *,
    data_root: Path,
    manifest: Manifest,
    max_items: int = DEFAULT_MAX_ITEMS,
    state: StateStore,
    storage_report: StorageReport,
    provider_module: Any | None = None,
    socket_module: ModuleType | None = None,
    protocol_constants: ModuleType | None = None,
) -> dict[str, object]:
    state.assert_fetch_ready()
    raw_store = ImmutableRawStore(data_root / "raw")
    selected, cache_hits = _select_items(
        manifest=manifest,
        raw_store=raw_store,
        state=state,
        max_items=max_items,
    )
    if not selected:
        return {
            "status": "complete",
            "manifest_id": manifest.manifest_id,
            "manifest_sha256": manifest.manifest_sha256,
            "network_attempts": 0,
            "fetched_items": [],
            "cache_hits": cache_hits,
            "storage": storage_report.as_dict(),
        }

    planned_attempts = 2 + sum(item.max_pages for item in selected)
    if planned_attempts > manifest.limits.session_attempts:
        raise BudgetExceeded("selected items plus login/logout exceed the session budget")

    module = provider_module or importlib.import_module("baostock")
    if getattr(module, "__version__", None) != PROVIDER_CLIENT_VERSION:
        raise ProviderFailure(
            f"BaoStock client version must be {PROVIDER_CLIENT_VERSION} for this manifest"
        )
    sock = socket_module or importlib.import_module("baostock.util.socketutil")
    constants = protocol_constants or importlib.import_module("baostock.common.contants")
    provider = BaoStockProvider(module)
    fetched: list[dict[str, object]] = []

    with GlobalProviderLock(state.root):
        state.assert_fetch_ready()
        state.assert_capacity(
            additional_attempts=planned_attempts,
            limits=manifest.limits,
        )
        session_id = state.start_session(
            manifest_sha256=manifest.manifest_sha256,
            data_root=data_root,
        )
        gate = SocketAttemptGate(
            state=state,
            session_id=session_id,
            manifest=manifest,
            socket_module=sock,
            protocol_constants=constants,
        )
        try:
            with gate.installed():
                gate.bind_login()
                provider.login()
                for item in selected:
                    gate.bind_item(item)
                    state.append_item_event(
                        manifest_sha256=manifest.manifest_sha256,
                        item_id=item.item_id,
                        event="fetching",
                        detail={"session_id": session_id},
                    )
                    batch = provider.fetch(item)
                    if batch.fields != item.expected_fields:
                        quarantine = _quarantine_schema_drift(
                            data_root=data_root,
                            manifest=manifest,
                            item=item,
                            batch=batch,
                        )
                        state.append_item_event(
                            manifest_sha256=manifest.manifest_sha256,
                            item_id=item.item_id,
                            event="quarantined",
                            detail={"path": str(quarantine), "reason": "schema_drift"},
                        )
                        raise SchemaDriftError(
                            f"{item.operation} fields differ from the frozen contract"
                        )
                    artifact = raw_store.persist(batch)
                    state.append_item_event(
                        manifest_sha256=manifest.manifest_sha256,
                        item_id=item.item_id,
                        event="raw_committed",
                        detail={
                            "raw_artifact": artifact.relative_path,
                            "content_sha256": artifact.content_sha256,
                            "rows": len(batch.rows),
                        },
                    )
                    fetched.append(
                        {
                            "item_id": item.item_id,
                            "operation": item.operation,
                            "rows": len(batch.rows),
                            "raw_artifact": artifact.relative_path,
                            "content_sha256": artifact.content_sha256,
                        }
                    )
                gate.bind_logout()
                provider.logout()
        except ProviderBlacklisted as exc:
            if gate.blacklist_incident_id is None:
                if gate.last_attempt_id is None:
                    raise StateError(
                        "provider blacklist was detected without a recorded socket attempt"
                    ) from exc
                gate.blacklist_incident_id = state.record_blacklist(
                    session_id=session_id,
                    attempt_id=gate.last_attempt_id,
                    detail={
                        "provider_code": BLACKLIST_ERROR_CODE,
                        "phase": "high_level_fallback",
                    },
                )
            provider.close_local_socket()
            state.append_session_event(
                session_id,
                "blacklisted",
                detail={
                    "incident_id": gate.blacklist_incident_id,
                    "reason": str(exc),
                },
            )
            state.mirror_to(data_root / "state" / "request-audit.sqlite")
            raise
        except Exception as exc:
            provider.close_local_socket()
            _append_blocked_item_event(
                state=state,
                manifest=manifest,
                selected=selected,
                fetched=fetched,
                reason=str(exc),
            )
            state.append_session_event(
                session_id,
                "failed",
                detail={"reason": str(exc), "exception": type(exc).__name__},
            )
            state.mirror_to(data_root / "state" / "request-audit.sqlite")
            raise
        else:
            state.append_session_event(
                session_id,
                "completed",
                detail={"fetched_items": len(fetched)},
            )
            state.mirror_to(data_root / "state" / "request-audit.sqlite")

    budget = state.budget_snapshot(manifest.limits)
    return {
        "status": "passed",
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.manifest_sha256,
        "session_id": session_id,
        "fetched_items": fetched,
        "cache_hits": cache_hits,
        "budget": budget.as_dict(),
        "storage": storage_report.as_dict(),
    }


def _select_items(
    *,
    manifest: Manifest,
    raw_store: ImmutableRawStore,
    state: StateStore,
    max_items: int,
) -> tuple[list[ManifestItem], int]:
    selected = []
    cache_hits = 0
    available: set[str] = set()
    statuses = state.item_statuses(manifest.manifest_sha256)
    for item in manifest.items:
        cached = raw_store.lookup_with_artifact(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
        )
        if cached is not None:
            batch, _ = cached
            if batch.fields != item.expected_fields:
                raise SchemaDriftError(
                    f"cached {item.operation} fields differ from the frozen contract"
                )
            available.add(item.item_id)
            cache_hits += 1
            continue
        if len(selected) >= max_items:
            continue
        missing_dependencies = [
            dependency for dependency in item.dependencies if dependency not in available
        ]
        if missing_dependencies:
            raise ManifestError(
                f"item {item.item_id} has incomplete dependencies: "
                f"{', '.join(missing_dependencies)}"
            )
        prior = statuses.get(item.item_id)
        if prior and prior["event"] in {"blocked", "quarantined"}:
            raise ManifestError(
                f"item {item.item_id} is {prior['event']}; freeze a reviewed retry manifest"
            )
        if state.item_attempt_count(manifest.manifest_sha256, item.item_id):
            raise ManifestError(
                f"item {item.item_id} has consumed attempts without committed raw; "
                "freeze a reviewed retry manifest"
            )
        selected.append(item)
        available.add(item.item_id)
    return selected, cache_hits


def _quarantine_schema_drift(
    *,
    data_root: Path,
    manifest: Manifest,
    item: ManifestItem,
    batch: ProviderBatch,
) -> Path:
    payload = {
        "schema_version": 1,
        "reason": "schema_drift",
        "manifest_sha256": manifest.manifest_sha256,
        "item_id": item.item_id,
        "operation": item.operation,
        "query": batch.query,
        "expected_fields": list(item.expected_fields),
        "actual_fields": list(batch.fields),
        "rows": list(batch.rows),
        "received_at": batch.received_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    content_sha256 = _content_sha(payload)
    path = (
        data_root
        / "quarantine"
        / manifest.manifest_id
        / item.item_id
        / f"{content_sha256}.json"
    )
    durable_write(path, canonical_json({**payload, "content_sha256": content_sha256}))
    return path


def _append_blocked_item_event(
    *,
    state: StateStore,
    manifest: Manifest,
    selected: list[ManifestItem],
    fetched: list[dict[str, object]],
    reason: str,
) -> None:
    completed = {str(item["item_id"]) for item in fetched}
    current = next((item for item in selected if item.item_id not in completed), None)
    if current is None:
        return
    statuses = state.item_statuses(manifest.manifest_sha256)
    if statuses.get(current.item_id, {}).get("event") == "quarantined":
        return
    state.append_item_event(
        manifest_sha256=manifest.manifest_sha256,
        item_id=current.item_id,
        event="blocked",
        detail={"reason": reason},
    )


def _content_sha(payload: dict[str, object]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(payload)).hexdigest()
