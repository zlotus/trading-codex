from fastapi import APIRouter

from trading_codex.api.routes.ai import router as ai_router
from trading_codex.api.routes.ledger import router as ledger_router
from trading_codex.api.routes.operations import router as operations_router
from trading_codex.api.routes.system import router as system_router

api_router = APIRouter()
api_router.include_router(system_router)
api_router.include_router(ledger_router)
api_router.include_router(ai_router)
api_router.include_router(operations_router)
