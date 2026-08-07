from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from trading_codex.api.schemas import ComponentStatus, HealthResponse, SystemStatusResponse
from trading_codex.config import get_settings

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(status="ok", service=settings.app_name, version=settings.version)


@router.get("/system/status", response_model=SystemStatusResponse)
async def system_status() -> SystemStatusResponse:
    settings = get_settings()
    return SystemStatusResponse(
        mode="scaffold",
        environment=settings.environment,
        server_time=datetime.now(ZoneInfo("Asia/Shanghai")),
        components=[
            ComponentStatus(
                key="historical_data",
                label="历史数据",
                state="not_configured",
                detail="BaoStock adapter is planned for Milestone 1.",
            ),
            ComponentStatus(
                key="realtime_quotes",
                label="实时行情",
                state="not_configured",
                detail="Opening-session quote adapters are not connected.",
            ),
            ComponentStatus(
                key="backtest",
                label="回测引擎",
                state="not_configured",
                detail="RQAlpha feasibility spike is pending.",
            ),
            ComponentStatus(
                key="ai",
                label="AI 协作",
                state="not_configured",
                detail="No model provider is configured.",
            ),
        ],
    )
