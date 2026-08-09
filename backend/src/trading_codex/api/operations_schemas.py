from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trading_codex.ledger.models import AlertPhase, AlertSeverity, ProviderHealthState


class OperationsModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProviderHealthResponse(OperationsModel):
    check_id: str
    provider: str
    state: ProviderHealthState
    critical: bool
    checked_at: datetime
    latency_ms: int
    detail: str
    metadata: dict[str, object]


class AlertResponse(OperationsModel):
    alert_key: str
    phase: AlertPhase
    active: bool
    severity: AlertSeverity
    message: str
    occurred_at: datetime
    source_check_id: str | None
    source_job_run_id: str | None
    context: dict[str, object]


class ForwardObservationResponse(OperationsModel):
    observation_id: str
    trading_date: date
    observed_at: datetime
    base_decision_id: str
    ai_shadow_decision_id: str
    snapshot_id: str
    base_configuration_id: str
    ai_shadow_configuration_id: str
    benchmark_return: Decimal
    base_target_return: Decimal
    base_simulated_return: Decimal
    ai_shadow_return: Decimal
    actual_return: Decimal
    transaction_cost_rate: Decimal
    source_payloads: list[str]
    metric_payload_sha256: str


class OperationsStatusResponse(BaseModel):
    as_of: datetime
    scheduler_mode: Literal["external_one_shot"] = "external_one_shot"
    scheduler_activated: Literal[False] = False
    health_gate_ready: bool
    health_max_age_seconds: int = 900
    provider_health: list[ProviderHealthResponse]
    active_alerts: list[AlertResponse]
    observed_trading_days: int
    required_trading_days: int = 60
    review_ready: bool
