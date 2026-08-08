from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, localcontext

from trading_codex.domain.contracts import (
    StrategyProposal,
    TargetPortfolio,
    TargetWeight,
)

ALLOCATION_VERSION = "inverse-volatility-allocation-v1"
WEIGHT_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class AllocationConfig:
    max_positions: int = 8
    max_position_weight: Decimal = Decimal("0.20")
    max_gross_exposure: Decimal = Decimal("0.95")
    version: str = ALLOCATION_VERSION

    def __post_init__(self) -> None:
        if self.max_positions < 1:
            raise ValueError("max positions must be positive")
        if not Decimal(0) < self.max_position_weight <= Decimal(1):
            raise ValueError("max position weight must be in (0, 1]")
        if not Decimal(0) <= self.max_gross_exposure <= Decimal(1):
            raise ValueError("max gross exposure must be in [0, 1]")
        if not self.version:
            raise ValueError("allocation version is required")


class TargetAllocator:
    def __init__(self, config: AllocationConfig | None = None) -> None:
        self.config = config or AllocationConfig()

    @property
    def version(self) -> str:
        return self.config.version

    def allocate(self, proposal: StrategyProposal) -> TargetPortfolio:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            selected = list(proposal.intents[: self.config.max_positions])
            assigned: dict[str, Decimal] = {}
            remaining_budget = self.config.max_gross_exposure
            remaining = selected

            while remaining and remaining_budget > 0:
                total_scale = sum(
                    (intent.inverse_volatility for intent in remaining), Decimal(0)
                )
                if total_scale <= 0:
                    break
                provisional = {
                    intent.code: remaining_budget * intent.inverse_volatility / total_scale
                    for intent in remaining
                }
                capped = [
                    intent
                    for intent in remaining
                    if provisional[intent.code] > self.config.max_position_weight
                ]
                if not capped:
                    assigned.update(provisional)
                    break
                for intent in capped:
                    assigned[intent.code] = self.config.max_position_weight
                    remaining_budget -= self.config.max_position_weight
                capped_codes = {intent.code for intent in capped}
                remaining = [
                    intent for intent in remaining if intent.code not in capped_codes
                ]

            ranks = {intent.code: intent.rank for intent in selected}
            weights = tuple(
                TargetWeight(
                    code=code,
                    weight=weight.quantize(WEIGHT_QUANTUM, rounding=ROUND_DOWN),
                    rank=ranks[code],
                )
                for code, weight in sorted(assigned.items())
                if weight > 0
            )
            gross = sum((target.weight for target in weights), Decimal(0))
        return TargetPortfolio(
            snapshot_id=proposal.snapshot_id,
            version=self.version,
            weights=weights,
            cash_weight=Decimal(1) - gross,
        )
