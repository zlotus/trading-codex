from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from trading_codex.domain.models import DecisionPoint, SnapshotValidationError


@dataclass(frozen=True)
class FeatureVector:
    code: str
    latest_trade_date: date
    momentum: Decimal
    annualized_volatility: Decimal
    risk_adjusted_momentum: Decimal
    short_term_return: Decimal
    observations: int

    def __post_init__(self) -> None:
        if self.annualized_volatility <= 0:
            raise SnapshotValidationError("feature volatility must be positive")
        if self.observations < 1:
            raise SnapshotValidationError("feature observations must be positive")


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

    def __post_init__(self) -> None:
        if self.rank < 1 or self.inverse_volatility <= 0:
            raise SnapshotValidationError(
                "strategy intent rank and inverse volatility must be positive"
            )


class StrategyKind(StrEnum):
    MOMENTUM = "momentum"
    SHORT_TERM_REVERSAL = "short_term_reversal"
    DEFENSIVE_LOW_VOLATILITY = "defensive_low_volatility"
    CASH = "cash"


class MarketRegimeLabel(StrEnum):
    RISK_ON = "risk_on"
    MEAN_REVERTING = "mean_reverting"
    DEFENSIVE = "defensive"
    RISK_OFF = "risk_off"


@dataclass(frozen=True)
class RegimeFeatureVector:
    trend_return: Decimal
    annualized_volatility: Decimal
    breadth: Decimal
    average_turnover: Decimal
    concentration: Decimal
    opening_return: Decimal
    universe_size: int
    daily_coverage: Decimal
    opening_coverage: Decimal

    def __post_init__(self) -> None:
        if self.annualized_volatility < 0 or self.average_turnover < 0:
            raise SnapshotValidationError("regime volatility and turnover must be non-negative")
        for name in ("breadth", "concentration", "daily_coverage", "opening_coverage"):
            value = getattr(self, name)
            if not Decimal(0) <= value <= Decimal(1):
                raise SnapshotValidationError(f"regime {name} must be in [0, 1]")
        if self.universe_size < 1:
            raise SnapshotValidationError("regime universe size must be positive")


@dataclass(frozen=True)
class RegimeProbability:
    label: MarketRegimeLabel
    probability: Decimal
    score: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.label, MarketRegimeLabel):
            raise SnapshotValidationError("market regime label is invalid")
        if not Decimal(0) <= self.probability <= Decimal(1):
            raise SnapshotValidationError("regime probability must be in [0, 1]")


@dataclass(frozen=True)
class MarketRegimeAssessment:
    snapshot_id: str
    as_of: datetime
    version: str
    features: RegimeFeatureVector
    probabilities: tuple[RegimeProbability, ...]
    selected: MarketRegimeLabel
    emergency_risk_off: bool
    explanations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise SnapshotValidationError("regime as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        labels = tuple(item.label for item in self.probabilities)
        if set(labels) != set(MarketRegimeLabel) or len(labels) != len(set(labels)):
            raise SnapshotValidationError("regime probabilities must cover each state once")
        if sum((item.probability for item in self.probabilities), Decimal(0)) != Decimal(1):
            raise SnapshotValidationError("regime probabilities must sum to one")
        if self.selected not in labels:
            raise SnapshotValidationError("selected regime is missing from probabilities")
        if self.emergency_risk_off and self.selected is not MarketRegimeLabel.RISK_OFF:
            raise SnapshotValidationError("emergency risk-off must select the risk-off state")
        if not self.version or not self.explanations:
            raise SnapshotValidationError("regime version and explanations are required")


@dataclass(frozen=True)
class StrategyProposal:
    snapshot_id: str
    strategy: StrategyKind
    version: str
    intents: tuple[StrategyIntent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, StrategyKind):
            raise SnapshotValidationError("strategy kind is invalid")
        if not self.version:
            raise SnapshotValidationError("strategy version is required")
        codes = tuple(intent.code for intent in self.intents)
        if len(codes) != len(set(codes)):
            raise SnapshotValidationError("strategy proposal contains duplicate instruments")


@dataclass(frozen=True)
class TargetWeight:
    code: str
    weight: Decimal
    rank: int


@dataclass(frozen=True)
class StrategyAllocation:
    strategy: StrategyKind
    weight: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, StrategyKind):
            raise SnapshotValidationError("strategy allocation kind is invalid")
        if not Decimal(0) < self.weight <= Decimal(1):
            raise SnapshotValidationError("strategy allocation weight must be in (0, 1]")


@dataclass(frozen=True)
class AllocationState:
    as_of: datetime
    active_strategy: StrategyKind
    weights: tuple[TargetWeight, ...]
    cash_weight: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.active_strategy, StrategyKind):
            raise SnapshotValidationError("allocation state strategy is invalid")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise SnapshotValidationError("allocation state as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        codes = tuple(weight.code for weight in self.weights)
        if codes != tuple(sorted(set(codes))):
            raise SnapshotValidationError("allocation state weights must be sorted and unique")
        if any(weight.weight <= 0 for weight in self.weights):
            raise SnapshotValidationError("allocation state weights must be positive")
        gross = sum((weight.weight for weight in self.weights), Decimal(0))
        if gross > Decimal(1) or not Decimal(0) <= self.cash_weight <= Decimal(1):
            raise SnapshotValidationError("allocation state exposure must be in [0, 1]")
        if self.cash_weight != Decimal(1) - gross:
            raise SnapshotValidationError("allocation state cash weight is inconsistent")


@dataclass(frozen=True)
class TargetPortfolio:
    snapshot_id: str
    version: str
    weights: tuple[TargetWeight, ...]
    cash_weight: Decimal
    active_strategy: StrategyKind = StrategyKind.MOMENTUM
    strategy_allocations: tuple[StrategyAllocation, ...] = ()
    turnover: Decimal = Decimal(0)
    emergency_risk_off: bool = False
    decision_point: DecisionPoint = DecisionPoint.EOD

    def __post_init__(self) -> None:
        if not isinstance(self.active_strategy, StrategyKind):
            raise SnapshotValidationError("target active strategy is invalid")
        if not isinstance(self.decision_point, DecisionPoint):
            raise SnapshotValidationError("target decision point is invalid")
        codes = tuple(weight.code for weight in self.weights)
        if codes != tuple(sorted(set(codes))):
            raise SnapshotValidationError("target weights must be sorted and unique")
        if any(weight.weight <= 0 for weight in self.weights):
            raise SnapshotValidationError("target weights must be positive")
        gross = sum((weight.weight for weight in self.weights), Decimal(0))
        if gross > Decimal(1) or self.cash_weight != Decimal(1) - gross:
            raise SnapshotValidationError("target portfolio exposure is inconsistent")
        if not Decimal(0) <= self.turnover <= Decimal(1):
            raise SnapshotValidationError("target turnover must be in [0, 1]")
        if self.strategy_allocations:
            strategies = tuple(item.strategy for item in self.strategy_allocations)
            if len(strategies) != len(set(strategies)):
                raise SnapshotValidationError("strategy allocations must be unique")
            if self.active_strategy not in strategies:
                raise SnapshotValidationError("active strategy is missing from allocations")
            if sum(
                (item.weight for item in self.strategy_allocations), Decimal(0)
            ) != Decimal(1):
                raise SnapshotValidationError("strategy allocations must sum to one")


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
    regime: MarketRegimeAssessment
    strategy_proposals: tuple[StrategyProposal, ...]
    proposal: StrategyProposal
    allocated: TargetPortfolio
    risk: RiskDecision
    execution: ExecutionPlan
    previous_allocation: AllocationState | None
    allocator_version: str

    @property
    def base_targets(self) -> tuple[TargetWeight, ...]:
        return self.risk.approved_weights


@dataclass(frozen=True)
class ReplayResult:
    configuration_id: str
    runs: tuple[DecisionRun, ...]
