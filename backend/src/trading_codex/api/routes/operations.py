from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from trading_codex.api.dependencies import get_ledger
from trading_codex.api.operations_schemas import (
    AlertResponse,
    ForwardObservationResponse,
    OperationsStatusResponse,
    ProviderHealthResponse,
)
from trading_codex.ledger.models import ProviderHealthState, as_utc
from trading_codex.ledger.store import SQLiteLedger

router = APIRouter(prefix="/operations", tags=["operations"])
LedgerDependency = Annotated[SQLiteLedger, Depends(get_ledger)]
HEALTH_MAX_AGE = timedelta(minutes=15)


@router.get("/status", response_model=OperationsStatusResponse)
async def operations_status(
    ledger: LedgerDependency,
    as_of: Annotated[datetime | None, Query()] = None,
) -> OperationsStatusResponse:
    current = as_utc(as_of or datetime.now(UTC), field="as_of")
    health = ledger.latest_provider_health(as_of=current)
    alerts = ledger.list_alerts(active_only=True, as_of=current)
    observations = ledger.list_forward_observations(as_of=current)
    critical = tuple(check for check in health if check.critical)
    health_gate_ready = bool(critical) and all(
        check.state is ProviderHealthState.HEALTHY
        and current - check.checked_at <= HEALTH_MAX_AGE
        for check in critical
    )
    return OperationsStatusResponse(
        as_of=current,
        health_gate_ready=health_gate_ready,
        provider_health=[ProviderHealthResponse.model_validate(check) for check in health],
        active_alerts=[AlertResponse.model_validate(alert) for alert in alerts],
        observed_trading_days=len(observations),
        review_ready=len(observations) >= 60,
    )


@router.get("/observations", response_model=list[ForwardObservationResponse])
async def list_observations(
    ledger: LedgerDependency,
    as_of: Annotated[datetime | None, Query()] = None,
) -> list[ForwardObservationResponse]:
    observations = ledger.list_forward_observations(as_of=as_of)
    return [ForwardObservationResponse.model_validate(item) for item in observations]
