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
        mode="research",
        environment=settings.environment,
        server_time=datetime.now(ZoneInfo("Asia/Shanghai")),
        components=[
            ComponentStatus(
                key="historical_data",
                label="历史数据",
                state="ready",
                detail="本地 BaoStock raw cache、normalized Parquet 和 as_of 查询已就绪。",
            ),
            ComponentStatus(
                key="decision_kernel",
                label="决策内核",
                state="ready",
                detail="确定性动量策略、分配、硬风险和执行计划已接入共享管线。",
            ),
            ComponentStatus(
                key="ledger",
                label="组合账本",
                state="ready",
                detail="append-only SQLite 事件、三轨持仓、人工成交和对账视图已就绪。",
            ),
            ComponentStatus(
                key="realtime_quotes",
                label="实时行情",
                state="not_configured",
                detail="开盘时段实时行情 adapter 尚未接入。",
            ),
            ComponentStatus(
                key="backtest",
                label="回测引擎",
                state="ready",
                detail="共享历史 replay 与隔离的 RQAlpha 6.3.0 日频适配器已就绪。",
            ),
            ComponentStatus(
                key="ai",
                label="AI 协作",
                state="not_configured",
                detail="尚未配置模型提供方，AI 不能参与当前决策。",
            ),
        ],
    )
