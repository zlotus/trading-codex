from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading_codex.api.ai_schemas import AIRunResponse, AIWorkspaceResponse
from trading_codex.api.dependencies import get_ledger
from trading_codex.ledger.models import LedgerInvariantError
from trading_codex.ledger.store import SQLiteLedger

router = APIRouter(prefix="/ai", tags=["ai"])
LedgerDependency = Annotated[SQLiteLedger, Depends(get_ledger)]


@router.get("/workspace", response_model=AIWorkspaceResponse)
async def workspace(
    ledger: LedgerDependency,
    as_of: Annotated[datetime | None, Query()] = None,
) -> AIWorkspaceResponse:
    try:
        latest = ledger.latest_ai_run(as_of=as_of)
    except LedgerInvariantError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    return AIWorkspaceResponse(
        provider_configured=False,
        latest=AIRunResponse.model_validate(latest) if latest is not None else None,
    )
