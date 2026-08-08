from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from trading_codex.domain.contracts import (
    FeatureSet,
    StrategyIntent,
    StrategyKind,
    StrategyProposal,
)
from trading_codex.strategies.momentum import VolatilityScaledMomentumStrategy

REVERSAL_VERSION = "short-term-reversal-v1"
LOW_VOLATILITY_VERSION = "defensive-low-volatility-v1"
CASH_VERSION = "cash-preservation-v1"


@dataclass(frozen=True)
class StrategyPoolConfig:
    candidate_count: int = 10

    def __post_init__(self) -> None:
        if self.candidate_count < 1:
            raise ValueError("strategy candidate count must be positive")


class ShortTermReversalStrategy:
    version = REVERSAL_VERSION

    def __init__(self, *, candidate_count: int = 10) -> None:
        self.candidate_count = candidate_count

    def propose(self, features: FeatureSet) -> StrategyProposal:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            ranked = sorted(
                (feature for feature in features.features if feature.short_term_return < 0),
                key=lambda feature: (
                    feature.short_term_return / feature.annualized_volatility,
                    feature.code,
                ),
            )[: self.candidate_count]
            intents = tuple(
                StrategyIntent(
                    code=feature.code,
                    rank=rank,
                    score=-feature.short_term_return / feature.annualized_volatility,
                    inverse_volatility=Decimal(1) / feature.annualized_volatility,
                )
                for rank, feature in enumerate(ranked, start=1)
            )
        return StrategyProposal(
            snapshot_id=features.snapshot_id,
            strategy=StrategyKind.SHORT_TERM_REVERSAL,
            version=self.version,
            intents=intents,
        )


class DefensiveLowVolatilityStrategy:
    version = LOW_VOLATILITY_VERSION

    def __init__(self, *, candidate_count: int = 10) -> None:
        self.candidate_count = candidate_count

    def propose(self, features: FeatureSet) -> StrategyProposal:
        ranked = sorted(
            features.features,
            key=lambda feature: (feature.annualized_volatility, feature.code),
        )[: self.candidate_count]
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            intents = tuple(
                StrategyIntent(
                    code=feature.code,
                    rank=rank,
                    score=Decimal(1) / feature.annualized_volatility,
                    inverse_volatility=Decimal(1) / feature.annualized_volatility,
                )
                for rank, feature in enumerate(ranked, start=1)
            )
        return StrategyProposal(
            snapshot_id=features.snapshot_id,
            strategy=StrategyKind.DEFENSIVE_LOW_VOLATILITY,
            version=self.version,
            intents=intents,
        )


class CashStrategy:
    version = CASH_VERSION

    def propose(self, features: FeatureSet) -> StrategyProposal:
        return StrategyProposal(
            snapshot_id=features.snapshot_id,
            strategy=StrategyKind.CASH,
            version=self.version,
            intents=(),
        )


class StrategyPool:
    def __init__(self, config: StrategyPoolConfig | None = None) -> None:
        self.config = config or StrategyPoolConfig()
        self.strategies = (
            VolatilityScaledMomentumStrategy(),
            ShortTermReversalStrategy(candidate_count=self.config.candidate_count),
            DefensiveLowVolatilityStrategy(candidate_count=self.config.candidate_count),
            CashStrategy(),
        )

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(strategy.version for strategy in self.strategies)

    def propose(self, features: FeatureSet) -> tuple[StrategyProposal, ...]:
        return tuple(strategy.propose(features) for strategy in self.strategies)
