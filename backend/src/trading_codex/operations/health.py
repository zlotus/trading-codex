from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic_ns
from typing import Protocol

from trading_codex.ledger.models import (
    AlertSeverity,
    ProviderHealthCheck,
    ProviderHealthState,
    as_utc,
)
from trading_codex.ledger.store import SQLiteLedger


class ProviderHealthGateError(RuntimeError):
    """One or more critical providers cannot support a daily run."""


@dataclass(frozen=True)
class ProbeResult:
    state: ProviderHealthState
    detail: str
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.state, ProviderHealthState):
            raise ValueError("probe state is invalid")
        if not self.detail.strip():
            raise ValueError("probe detail is required")


class ProviderProbe(Protocol):
    name: str
    critical: bool

    def check(self, *, as_of: datetime) -> ProbeResult: ...


@dataclass(frozen=True)
class HealthSummary:
    checked_at: datetime
    checks: tuple[ProviderHealthCheck, ...]

    @property
    def blocking(self) -> tuple[ProviderHealthCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if check.critical and check.state is not ProviderHealthState.HEALTHY
        )

    @property
    def critical(self) -> tuple[ProviderHealthCheck, ...]:
        return tuple(check for check in self.checks if check.critical)

    @property
    def ready(self) -> bool:
        return bool(self.critical) and not self.blocking

    def require_ready(self) -> None:
        if self.ready:
            return
        if not self.critical:
            raise ProviderHealthGateError("no critical provider probes are configured")
        failures = ", ".join(f"{check.provider}={check.state.value}" for check in self.blocking)
        raise ProviderHealthGateError(f"critical provider health gate failed: {failures}")


class ProviderHealthMonitor:
    def __init__(self, ledger: SQLiteLedger) -> None:
        self.ledger = ledger

    def check(
        self,
        probes: tuple[ProviderProbe, ...],
        *,
        checked_at: datetime,
        source_job_run_id: str | None = None,
    ) -> HealthSummary:
        checked = as_utc(checked_at, field="checked_at")
        names = tuple(probe.name for probe in probes)
        if len(names) != len(set(names)):
            raise ValueError("provider probe names must be unique")

        checks: list[ProviderHealthCheck] = []
        for probe in probes:
            started = monotonic_ns()
            try:
                result = probe.check(as_of=checked)
            except Exception as error:
                result = ProbeResult(
                    state=ProviderHealthState.UNAVAILABLE,
                    detail=f"{type(error).__name__}: {error}"[:1000],
                )
            latency_ms = max(0, (monotonic_ns() - started) // 1_000_000)
            check = self.ledger.record_provider_health(
                provider=probe.name,
                state=result.state,
                critical=probe.critical,
                checked_at=checked,
                latency_ms=latency_ms,
                detail=result.detail,
                metadata=result.metadata,
            )
            checks.append(check)
            alert_active = result.state in {
                ProviderHealthState.DEGRADED,
                ProviderHealthState.UNAVAILABLE,
            } or (probe.critical and result.state is ProviderHealthState.NOT_CONFIGURED)
            if alert_active:
                severity = (
                    AlertSeverity.CRITICAL
                    if probe.critical
                    else AlertSeverity.WARNING
                )
                self.ledger.transition_alert(
                    alert_key=f"provider:{probe.name}",
                    active=True,
                    severity=severity,
                    message=result.detail,
                    occurred_at=checked,
                    source_check_id=check.check_id,
                    source_job_run_id=source_job_run_id,
                    context={"provider": probe.name, "state": result.state.value},
                )
            else:
                recovered_message = (
                    f"{probe.name} provider health recovered"
                    if result.state is ProviderHealthState.HEALTHY
                    else f"{probe.name} optional provider is not configured"
                )
                self.ledger.transition_alert(
                    alert_key=f"provider:{probe.name}",
                    active=False,
                    severity=AlertSeverity.WARNING,
                    message=recovered_message,
                    occurred_at=checked,
                    source_check_id=check.check_id,
                    source_job_run_id=source_job_run_id,
                    context={"provider": probe.name, "state": result.state.value},
                )
        return HealthSummary(checked_at=checked, checks=tuple(checks))


@dataclass(frozen=True)
class SQLiteIntegrityProbe:
    path: Path
    name: str = "ledger"
    critical: bool = True

    def check(self, *, as_of: datetime) -> ProbeResult:
        as_utc(as_of, field="as_of")
        resolved = self.path.resolve()
        if not resolved.is_file():
            return ProbeResult(
                state=ProviderHealthState.UNAVAILABLE,
                detail="ledger database does not exist",
                metadata={"path": str(resolved)},
            )
        connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=5)
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
        if integrity != "ok" or foreign_keys:
            return ProbeResult(
                state=ProviderHealthState.UNAVAILABLE,
                detail="ledger integrity or foreign-key check failed",
                metadata={
                    "integrity": str(integrity),
                    "foreign_key_errors": len(foreign_keys),
                    "schema_version": schema_version,
                },
            )
        return ProbeResult(
            state=ProviderHealthState.HEALTHY,
            detail="ledger quick_check and foreign-key check passed",
            metadata={"schema_version": schema_version},
        )


@dataclass(frozen=True)
class NotConfiguredProbe:
    name: str
    critical: bool = False
    detail: str = "provider adapter is not configured"

    def check(self, *, as_of: datetime) -> ProbeResult:
        as_utc(as_of, field="as_of")
        return ProbeResult(state=ProviderHealthState.NOT_CONFIGURED, detail=self.detail)
