from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading_codex.api.dependencies import get_ledger
from trading_codex.api.ledger_schemas import (
    JobRunResponse,
    LedgerDashboardResponse,
    RecordCashMovementRequest,
    RecordFillRequest,
    SignalDetailResponse,
    SkipSignalRequest,
)
from trading_codex.ledger.models import (
    LedgerConflictError,
    LedgerInvariantError,
    LedgerNotFoundError,
)
from trading_codex.ledger.store import SQLiteLedger

router = APIRouter(prefix="/ledger", tags=["ledger"])
LedgerDependency = Annotated[SQLiteLedger, Depends(get_ledger)]


@router.get("/dashboard", response_model=LedgerDashboardResponse)
async def dashboard(
    ledger: LedgerDependency,
    as_of: Annotated[datetime | None, Query()] = None,
) -> LedgerDashboardResponse:
    try:
        view = ledger.dashboard(as_of=as_of)
    except LedgerInvariantError as error:
        raise _unprocessable(error) from error
    return LedgerDashboardResponse.model_validate(view)


@router.get("/signals/{signal_id}", response_model=SignalDetailResponse)
async def signal_detail(
    signal_id: str,
    ledger: LedgerDependency,
    as_of: Annotated[datetime | None, Query()] = None,
) -> SignalDetailResponse:
    try:
        detail = ledger.signal_detail(signal_id, as_of=as_of)
    except LedgerNotFoundError as error:
        raise _not_found(error) from error
    return SignalDetailResponse.model_validate(detail)


@router.post(
    "/cash-movements",
    response_model=LedgerDashboardResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_cash_movement(
    request: RecordCashMovementRequest,
    ledger: LedgerDependency,
) -> LedgerDashboardResponse:
    try:
        ledger.record_cash_movement(
            portfolio_track=request.portfolio_track,
            kind=request.kind,
            amount=request.amount,
            occurred_at=request.occurred_at,
            idempotency_key=request.idempotency_key,
            note=request.note,
        )
        view = ledger.dashboard(as_of=max(request.occurred_at, datetime.now(UTC)))
    except LedgerConflictError as error:
        raise _conflict(error) from error
    except LedgerInvariantError as error:
        raise _unprocessable(error) from error
    return LedgerDashboardResponse.model_validate(view)


@router.post(
    "/fills",
    response_model=SignalDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_fill(
    request: RecordFillRequest,
    ledger: LedgerDependency,
) -> SignalDetailResponse:
    try:
        fill = ledger.record_fill(
            source_order_intent_id=request.source_order_intent_id,
            portfolio_track=request.portfolio_track,
            quantity=request.quantity,
            price=request.price,
            fees=request.fees,
            occurred_at=request.occurred_at,
            idempotency_key=request.idempotency_key,
            note=request.note,
        )
        detail = ledger.signal_detail(
            ledger.signal_id_for_order_intent(fill.source_order_intent_id),
            as_of=max(request.occurred_at, datetime.now(UTC)),
        )
    except LedgerNotFoundError as error:
        raise _not_found(error) from error
    except LedgerConflictError as error:
        raise _conflict(error) from error
    except LedgerInvariantError as error:
        raise _unprocessable(error) from error
    return SignalDetailResponse.model_validate(detail)


@router.post("/signals/{signal_id}/skip", response_model=SignalDetailResponse)
async def skip_signal(
    signal_id: str,
    request: SkipSignalRequest,
    ledger: LedgerDependency,
) -> SignalDetailResponse:
    try:
        ledger.skip_signal(
            signal_id,
            portfolio_track=request.portfolio_track,
            reason=request.reason,
            occurred_at=request.occurred_at,
            idempotency_key=request.idempotency_key,
        )
        detail = ledger.signal_detail(
            signal_id,
            as_of=max(request.occurred_at, datetime.now(UTC)),
        )
    except LedgerNotFoundError as error:
        raise _not_found(error) from error
    except LedgerConflictError as error:
        raise _conflict(error) from error
    except LedgerInvariantError as error:
        raise _unprocessable(error) from error
    return SignalDetailResponse.model_validate(detail)


@router.get("/jobs", response_model=list[JobRunResponse])
async def list_jobs(
    ledger: LedgerDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[JobRunResponse]:
    return [JobRunResponse.model_validate(run) for run in ledger.list_job_runs(limit=limit)]


def _not_found(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


def _unprocessable(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=str(error),
    )
