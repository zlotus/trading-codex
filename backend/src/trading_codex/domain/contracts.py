from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


@dataclass(frozen=True)
class FeatureVector:
    code: str
    latest_trade_date: date
    momentum: Decimal
    annualized_volatility: Decimal
    risk_adjusted_momentum: Decimal
    observations: int


@dataclass(frozen=True)
class FeatureExclusion:
    code: str
    reason: str


@dataclass(frozen=True)
class Candidate:
    code: str
    rank: int
    momentum: Decimal
    annualized_volatility: Decimal
    score: Decimal


@dataclass(frozen=True)
class FeatureSet:
    snapshot_id: str
    as_of: datetime
    version: str
    features: tuple[FeatureVector, ...]
    candidates: tuple[Candidate, ...]
    exclusions: tuple[FeatureExclusion, ...]


@dataclass(frozen=True)
class StrategyIntent:
    code: str
    rank: int
    score: Decimal
    inverse_volatility: Decimal


@dataclass(frozen=True)
class StrategyProposal:
    snapshot_id: str
    version: str
    intents: tuple[StrategyIntent, ...]


@dataclass(frozen=True)
class TargetWeight:
    code: str
    weight: Decimal
    rank: int


@dataclass(frozen=True)
class TargetPortfolio:
    snapshot_id: str
    version: str
    weights: tuple[TargetWeight, ...]
    cash_weight: Decimal


@dataclass(frozen=True)
class RiskRejection:
    code: str
    reason: str
    requested_weight: Decimal
    approved_weight: Decimal


@dataclass(frozen=True)
class RiskDecision:
    snapshot_id: str
    version: str
    equity: Decimal
    requested: TargetPortfolio
    approved_weights: tuple[TargetWeight, ...]
    rejections: tuple[RiskRejection, ...]


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class PlannedOrder:
    code: str
    side: OrderSide
    quantity: int
    reference_price: Decimal
    estimated_fees: Decimal
    target_weight: Decimal
    expires_at: datetime


@dataclass(frozen=True)
class ExecutionPlan:
    snapshot_id: str
    version: str
    orders: tuple[PlannedOrder, ...]
    estimated_cash_after_orders: Decimal


@dataclass(frozen=True)
class DecisionRun:
    decision_id: str
    snapshot_id: str
    configuration_id: str
    pipeline_version: str
    features: FeatureSet
    proposal: StrategyProposal
    allocated: TargetPortfolio
    risk: RiskDecision
    execution: ExecutionPlan

    @property
    def base_targets(self) -> tuple[TargetWeight, ...]:
        return self.risk.approved_weights


@dataclass(frozen=True)
class ReplayResult:
    configuration_id: str
    runs: tuple[DecisionRun, ...]
