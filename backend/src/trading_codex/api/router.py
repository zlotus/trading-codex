from fastapi import APIRouter

from trading_codex.api.routes.system import router as system_router

api_router = APIRouter()
api_router.include_router(system_router)
