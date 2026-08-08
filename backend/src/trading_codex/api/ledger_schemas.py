from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trading_codex.ledger.models import (
    CashMovementKind,
    JobStatus,
    JobType,
    PortfolioTrack,
    SignalStatus,
)


class LedgerModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PositionResponse(LedgerModel):
    code: str
    quantity: int
    sellable_quantity: int
    average_cost: Decimal
    last_price: Decimal | None
    market_value: Decimal | None


class TrackResponse(LedgerModel):
    track: PortfolioTrack
    cash: Decimal
    market_value: Decimal | None
    equity: Decimal | None
    positions: list[PositionResponse]


class SignalResponse(LedgerModel):
    signal_id: str
    order_intent_id: str
    decision_id: str
    snapshot_id: str
    portfolio_track: PortfolioTrack
    code: str
    side: Literal["buy", "sell"]
    suggested_quantity: int
    filled_quantity: int
    remaining_quantity: int
    reference_price: Decimal
    target_weight: Decimal
    estimated_fees: Decimal
    expires_at: datetime
    status: SignalStatus
    skip_reason: str | None


class PricePointResponse(LedgerModel):
    trade_date: date
    signal_close: Decimal | None
    execution_close: Decimal | None


class SignalTraceResponse(LedgerModel):
    decision_id: str
    snapshot_id: str
    configuration_id: str
    pipeline_version: str
    regime_version: str
    allocator_version: str
    source_payloads: list[str]
    recorded_at: datetime


class SignalDetailResponse(LedgerModel):
    signal: SignalResponse
    price_points: list[PricePointResponse]
    trace: SignalTraceResponse


class ReconciliationRowResponse(LedgerModel):
    code: str
    base_quantity: int
    ai_shadow_quantity: int
    actual_quantity: int
    actual_vs_base: int


class ReconciliationResponse(LedgerModel):
    cash_actual_vs_base: Decimal
    equity_actual_vs_base: Decimal | None
    rows: list[ReconciliationRowResponse]


class LedgerDashboardResponse(LedgerModel):
    as_of: datetime
    tracks: list[TrackResponse]
    signals: list[SignalResponse]
    reconciliation: ReconciliationResponse


class RecordCashMovementRequest(BaseModel):
    portfolio_track: Literal[PortfolioTrack.ACTUAL] = PortfolioTrack.ACTUAL
    kind: Literal[CashMovementKind.DEPOSIT, CashMovementKind.WITHDRAWAL]
    amount: Decimal = Field(gt=0)
    occurred_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class RecordFillRequest(BaseModel):
    source_order_intent_id: str = Field(min_length=1)
    portfolio_track: Literal[PortfolioTrack.ACTUAL] = PortfolioTrack.ACTUAL
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)
    fees: Decimal = Field(ge=0)
    occurred_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class SkipSignalRequest(BaseModel):
    portfolio_track: Literal[PortfolioTrack.ACTUAL] = PortfolioTrack.ACTUAL
    reason: str = Field(min_length=1, max_length=500)
    occurred_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=200)


class JobRunResponse(LedgerModel):
    run_id: str
    job_type: JobType
    scheduled_for: datetime
    status: JobStatus
    attempts: int
    latest_error: str | None
    latest_result: dict[str, object] | None
