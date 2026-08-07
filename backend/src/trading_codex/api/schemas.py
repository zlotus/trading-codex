from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ComponentState = Literal["ready", "not_configured", "degraded"]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


class ComponentStatus(BaseModel):
    key: str
    label: str
    state: ComponentState
    detail: str


class SystemStatusResponse(BaseModel):
    mode: Literal["scaffold"]
    environment: str
    server_time: datetime
    components: list[ComponentStatus]
