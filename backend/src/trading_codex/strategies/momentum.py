from decimal import ROUND_HALF_EVEN, Context, localcontext

from trading_codex.domain.contracts import (
    FeatureSet,
    StrategyIntent,
    StrategyKind,
    StrategyProposal,
)

STRATEGY_VERSION = "volatility-scaled-cross-sectional-momentum-v1"


class VolatilityScaledMomentumStrategy:
    version = STRATEGY_VERSION

    def propose(self, features: FeatureSet) -> StrategyProposal:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            intents = tuple(
                StrategyIntent(
                    code=candidate.code,
                    rank=candidate.rank,
                    score=candidate.score,
                    inverse_volatility=1 / candidate.annualized_volatility,
                )
                for candidate in features.candidates
            )
        return StrategyProposal(
            snapshot_id=features.snapshot_id,
            strategy=StrategyKind.MOMENTUM,
            version=self.version,
            intents=intents,
        )
