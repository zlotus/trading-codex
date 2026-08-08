from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum


class LedgerError(RuntimeError):
    """Base error for append-only portfolio accounting."""


class LedgerInvariantError(LedgerError):
    """An event would make cash, positions, or traceability inconsistent."""


class LedgerConflictError(LedgerError):
    """An idempotency key was reused for a different event."""


class LedgerNotFoundError(LedgerError):
    """A referenced decision, signal, or order intent does not exist."""


class PortfolioTrack(StrEnum):
    BASE = "base"
    AI_SHADOW = "ai_shadow"
    ACTUAL = "actual"


class CashMovementKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRADE = "trade"
    FEE = "fee"


class SignalStatus(StrEnum):
    ACTIVE = "active"
    PARTIAL = "partial"
    FILLED = "filled"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class JobType(StrEnum):
    EOD_PREPARATION = "eod_preparation"
    OPENING_DECISION = "opening_decision"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class PositionView:
    code: str
    quantity: int
    sellable_quantity: int
    average_cost: Decimal
    last_price: Decimal | None
    market_value: Decimal | None


@dataclass(frozen=True)
class TrackView:
    track: PortfolioTrack
    cash: Decimal
    market_value: Decimal | None
    equity: Decimal | None
    positions: tuple[PositionView, ...]


@dataclass(frozen=True)
class SignalView:
    signal_id: str
    order_intent_id: str
    decision_id: str
    snapshot_id: str
    portfolio_track: PortfolioTrack
    code: str
    side: str
    suggested_quantity: int
    filled_quantity: int
    remaining_quantity: int
    reference_price: Decimal
    target_weight: Decimal
    estimated_fees: Decimal
    expires_at: datetime
    status: SignalStatus
    skip_reason: str | None


@dataclass(frozen=True)
class PricePoint:
    trade_date: date
    signal_close: Decimal | None
    execution_close: Decimal | None


@dataclass(frozen=True)
class SignalTrace:
    decision_id: str
    snapshot_id: str
    configuration_id: str
    pipeline_version: str
    source_payloads: tuple[str, ...]
    recorded_at: datetime


@dataclass(frozen=True)
class SignalDetail:
    signal: SignalView
    price_points: tuple[PricePoint, ...]
    trace: SignalTrace


@dataclass(frozen=True)
class ReconciliationRow:
    code: str
    base_quantity: int
    ai_shadow_quantity: int
    actual_quantity: int
    actual_vs_base: int


@dataclass(frozen=True)
class ReconciliationView:
    cash_actual_vs_base: Decimal
    equity_actual_vs_base: Decimal | None
    rows: tuple[ReconciliationRow, ...]


@dataclass(frozen=True)
class LedgerDashboard:
    as_of: datetime
    tracks: tuple[TrackView, ...]
    signals: tuple[SignalView, ...]
    reconciliation: ReconciliationView


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    source_order_intent_id: str
    portfolio_track: PortfolioTrack
    code: str
    side: str
    quantity: int
    price: Decimal
    fees: Decimal
    occurred_at: datetime
    note: str | None


@dataclass(frozen=True)
class CashMovementRecord:
    movement_id: str
    portfolio_track: PortfolioTrack
    kind: CashMovementKind
    amount: Decimal
    occurred_at: datetime
    fill_id: str | None
    note: str | None


@dataclass(frozen=True)
class JobRunView:
    run_id: str
    job_type: JobType
    scheduled_for: datetime
    status: JobStatus
    attempts: int
    latest_error: str | None
    latest_result: dict[str, object] | None


def as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LedgerInvariantError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
