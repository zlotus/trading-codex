from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, localcontext

from trading_codex.domain.contracts import (
    AllocationState,
    MarketRegimeAssessment,
    MarketRegimeLabel,
    StrategyAllocation,
    StrategyKind,
    StrategyProposal,
    TargetPortfolio,
    TargetWeight,
)
from trading_codex.domain.models import DecisionPoint, DecisionSnapshot, SnapshotValidationError
from trading_codex.portfolio.allocation import WEIGHT_QUANTUM, AllocationConfig, TargetAllocator

REGIME_ALLOCATION_VERSION = "regime-constrained-allocation-v1"

REGIME_STRATEGY = {
    MarketRegimeLabel.RISK_ON: StrategyKind.MOMENTUM,
    MarketRegimeLabel.MEAN_REVERTING: StrategyKind.SHORT_TERM_REVERSAL,
    MarketRegimeLabel.DEFENSIVE: StrategyKind.DEFENSIVE_LOW_VOLATILITY,
    MarketRegimeLabel.RISK_OFF: StrategyKind.CASH,
}
STRATEGY_REGIME = {strategy: regime for regime, strategy in REGIME_STRATEGY.items()}


@dataclass(frozen=True)
class RegimeAllocationConfig:
    base: AllocationConfig = field(default_factory=AllocationConfig)
    switch_hysteresis: Decimal = Decimal("0.08")
    max_turnover: Decimal = Decimal("0.20")
    strategy_change_points: tuple[DecisionPoint, ...] = (DecisionPoint.OPENING_0935,)
    version: str = REGIME_ALLOCATION_VERSION

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.switch_hysteresis <= Decimal(1):
            raise ValueError("switch hysteresis must be in [0, 1]")
        if not Decimal(0) <= self.max_turnover <= Decimal(1):
            raise ValueError("max turnover must be in [0, 1]")
        if not self.strategy_change_points:
            raise ValueError("at least one strategy change point is required")
        if len(self.strategy_change_points) != len(set(self.strategy_change_points)):
            raise ValueError("strategy change points must be unique")
        if not self.version:
            raise ValueError("allocation version is required")


class RegimeAwareAllocator:
    def __init__(self, config: RegimeAllocationConfig | None = None) -> None:
        self.config = config or RegimeAllocationConfig()
        self.target_allocator = TargetAllocator(self.config.base)

    @property
    def version(self) -> str:
        return self.config.version

    def allocate(
        self,
        snapshot: DecisionSnapshot,
        regime: MarketRegimeAssessment,
        proposals: tuple[StrategyProposal, ...],
        *,
        previous: AllocationState | None = None,
    ) -> TargetPortfolio:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            self._validate_inputs(snapshot, regime, proposals, previous)
            by_strategy = {proposal.strategy: proposal for proposal in proposals}
            active = self._active_strategy(snapshot, regime, previous)
            target_strategy = StrategyKind.CASH if regime.emergency_risk_off else active
            desired = self.target_allocator.allocate(by_strategy[target_strategy])
            desired_weights = desired.weights

            if previous is None or regime.emergency_risk_off:
                weights = desired_weights
            else:
                weights = self._limit_turnover(previous, desired_weights)
            cash_weight = Decimal(1) - sum(
                (item.weight for item in weights), Decimal(0)
            )
            turnover = _turnover(previous, weights, cash_weight)
            return TargetPortfolio(
                snapshot_id=snapshot.snapshot_id,
                version=self.version,
                weights=weights,
                cash_weight=cash_weight,
                active_strategy=active,
                strategy_allocations=(
                    StrategyAllocation(strategy=active, weight=Decimal(1)),
                ),
                turnover=turnover,
                emergency_risk_off=regime.emergency_risk_off,
                decision_point=snapshot.decision_point,
            )

    def _active_strategy(
        self,
        snapshot: DecisionSnapshot,
        regime: MarketRegimeAssessment,
        previous: AllocationState | None,
    ) -> StrategyKind:
        recommended = REGIME_STRATEGY[regime.selected]
        if previous is None or previous.active_strategy is recommended:
            return recommended
        if snapshot.decision_point not in self.config.strategy_change_points:
            return previous.active_strategy
        if regime.emergency_risk_off:
            return StrategyKind.CASH

        probabilities = {item.label: item.probability for item in regime.probabilities}
        previous_regime = STRATEGY_REGIME[previous.active_strategy]
        advantage = probabilities[regime.selected] - probabilities[previous_regime]
        return (
            recommended
            if advantage >= self.config.switch_hysteresis
            else previous.active_strategy
        )

    def _limit_turnover(
        self,
        previous: AllocationState,
        desired: tuple[TargetWeight, ...],
    ) -> tuple[TargetWeight, ...]:
        desired_cash = Decimal(1) - sum((item.weight for item in desired), Decimal(0))
        full_turnover = _turnover(previous, desired, desired_cash)
        if full_turnover <= self.config.max_turnover or full_turnover == 0:
            return desired

        old = {item.code: item for item in previous.weights}
        target = {item.code: item for item in desired}
        codes = sorted(set(old) | set(target))
        rounding_reserve = WEIGHT_QUANTUM * Decimal(len(codes) + 1)
        if self.config.max_turnover <= rounding_reserve:
            return previous.weights
        scale = (self.config.max_turnover - rounding_reserve) / full_turnover
        weights = []
        for code in codes:
            old_weight = old.get(code)
            target_weight = target.get(code)
            before = old_weight.weight if old_weight is not None else Decimal(0)
            after = target_weight.weight if target_weight is not None else Decimal(0)
            weight = (before + (after - before) * scale).quantize(
                WEIGHT_QUANTUM,
                rounding=ROUND_DOWN,
            )
            if weight <= 0:
                continue
            rank = (
                target_weight.rank
                if target_weight is not None
                else old_weight.rank if old_weight is not None else 10_000
            )
            weights.append(TargetWeight(code=code, weight=weight, rank=rank))
        result = tuple(weights)
        cash_weight = Decimal(1) - sum((item.weight for item in result), Decimal(0))
        if _turnover(previous, result, cash_weight) > self.config.max_turnover:
            raise SnapshotValidationError("quantized target exceeds turnover limit")
        return result

    def _validate_inputs(
        self,
        snapshot: DecisionSnapshot,
        regime: MarketRegimeAssessment,
        proposals: tuple[StrategyProposal, ...],
        previous: AllocationState | None,
    ) -> None:
        if regime.snapshot_id != snapshot.snapshot_id or regime.as_of != snapshot.as_of:
            raise SnapshotValidationError("regime assessment belongs to a different snapshot")
        expected = set(StrategyKind)
        actual = {proposal.strategy for proposal in proposals}
        if actual != expected or len(proposals) != len(expected):
            raise SnapshotValidationError("strategy pool is incomplete or duplicated")
        if any(proposal.snapshot_id != snapshot.snapshot_id for proposal in proposals):
            raise SnapshotValidationError("strategy proposal belongs to a different snapshot")
        if previous is not None:
            if previous.as_of >= snapshot.as_of:
                raise SnapshotValidationError("previous allocation must precede snapshot as_of")
            if previous.active_strategy not in expected:
                raise SnapshotValidationError("previous allocation uses an unknown strategy")
            gross = sum((item.weight for item in previous.weights), Decimal(0))
            if gross > self.config.base.max_gross_exposure:
                raise SnapshotValidationError("previous allocation exceeds gross exposure limit")
            if any(
                item.weight > self.config.base.max_position_weight
                for item in previous.weights
            ):
                raise SnapshotValidationError("previous allocation exceeds position limit")


def allocation_state(as_of: datetime, target: TargetPortfolio) -> AllocationState:
    return AllocationState(
        as_of=as_of,
        active_strategy=target.active_strategy,
        weights=target.weights,
        cash_weight=target.cash_weight,
    )


def _turnover(
    previous: AllocationState | None,
    weights: tuple[TargetWeight, ...],
    cash_weight: Decimal,
) -> Decimal:
    old = {item.code: item.weight for item in previous.weights} if previous else {}
    new = {item.code: item.weight for item in weights}
    previous_cash = previous.cash_weight if previous else Decimal(1)
    absolute_change = sum(
        (
            abs(new.get(code, Decimal(0)) - old.get(code, Decimal(0)))
            for code in set(old) | set(new)
        ),
        Decimal(0),
    ) + abs(cash_weight - previous_cash)
    return (absolute_change / Decimal(2)).quantize(
        WEIGHT_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )
