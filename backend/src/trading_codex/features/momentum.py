from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from trading_codex.domain.contracts import (
    Candidate,
    FeatureExclusion,
    FeatureSet,
    FeatureVector,
)
from trading_codex.domain.models import DailyBar, DecisionSnapshot

FEATURE_VERSION = "momentum-volatility-features-v1"
FEATURE_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True)
class MomentumFeatureConfig:
    momentum_lookback: int = 20
    volatility_lookback: int = 20
    annualization_days: int = 252
    candidate_count: int = 10
    minimum_momentum: Decimal = Decimal(0)
    version: str = FEATURE_VERSION

    def __post_init__(self) -> None:
        if self.momentum_lookback < 1:
            raise ValueError("momentum lookback must be positive")
        if self.volatility_lookback < 2:
            raise ValueError("volatility lookback must be at least two")
        if self.annualization_days < 1:
            raise ValueError("annualization days must be positive")
        if self.candidate_count < 1:
            raise ValueError("candidate count must be positive")
        if not self.version:
            raise ValueError("feature version is required")


class MomentumFeaturePipeline:
    def __init__(self, config: MomentumFeatureConfig | None = None) -> None:
        self.config = config or MomentumFeatureConfig()

    @property
    def version(self) -> str:
        return self.config.version

    def compute(self, snapshot: DecisionSnapshot) -> FeatureSet:
        features: list[FeatureVector] = []
        exclusions: list[FeatureExclusion] = []
        required = max(
            self.config.momentum_lookback,
            self.config.volatility_lookback,
        ) + 1

        for code in snapshot.candidate_codes:
            current = snapshot.state_on(code, snapshot.decision_date)
            reason = _current_exclusion(current)
            if reason is not None:
                exclusions.append(FeatureExclusion(code=code, reason=reason))
                continue
            priced = [
                bar
                for bar in snapshot.bars_for(code)
                if bar.trade_status and bar.signal_close is not None
            ]
            if len(priced) < required:
                exclusions.append(FeatureExclusion(code=code, reason="insufficient_history"))
                continue

            prices = [bar.signal_close for bar in priced]
            assert all(price is not None for price in prices)
            numeric_prices = [price for price in prices if price is not None]
            vector = self._vector(code, priced[-1].trade_date, numeric_prices)
            if vector is None:
                exclusions.append(FeatureExclusion(code=code, reason="zero_volatility"))
                continue
            features.append(vector)

        ranked = sorted(
            (
                feature
                for feature in features
                if feature.momentum > self.config.minimum_momentum
            ),
            key=lambda feature: (-feature.risk_adjusted_momentum, feature.code),
        )[: self.config.candidate_count]
        candidates = tuple(
            Candidate(
                code=feature.code,
                rank=rank,
                momentum=feature.momentum,
                annualized_volatility=feature.annualized_volatility,
                score=feature.risk_adjusted_momentum,
            )
            for rank, feature in enumerate(ranked, start=1)
        )
        return FeatureSet(
            snapshot_id=snapshot.snapshot_id,
            as_of=snapshot.as_of,
            version=self.version,
            features=tuple(sorted(features, key=lambda feature: feature.code)),
            candidates=candidates,
            exclusions=tuple(sorted(exclusions, key=lambda exclusion: exclusion.code)),
        )

    def _vector(
        self,
        code: str,
        latest_trade_date: date,
        prices: list[Decimal],
    ) -> FeatureVector | None:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            momentum = prices[-1] / prices[-self.config.momentum_lookback - 1] - 1
            volatility_prices = prices[-self.config.volatility_lookback - 1 :]
            returns = [
                current / previous - 1
                for previous, current in zip(
                    volatility_prices,
                    volatility_prices[1:],
                )
            ]
            mean = sum(returns, Decimal(0)) / Decimal(len(returns))
            variance = sum((value - mean) ** 2 for value in returns) / Decimal(
                len(returns) - 1
            )
            volatility = variance.sqrt() * Decimal(self.config.annualization_days).sqrt()
            if volatility == 0:
                return None
            score = momentum / volatility
            quantized_volatility = _quantize(volatility)
            if quantized_volatility == 0:
                return None
            return FeatureVector(
                code=code,
                latest_trade_date=latest_trade_date,
                momentum=_quantize(momentum),
                annualized_volatility=quantized_volatility,
                risk_adjusted_momentum=_quantize(score),
                observations=len(prices),
            )


def _current_exclusion(current: DailyBar | None) -> str | None:
    if current is None:
        return "missing_current_bar"
    if not current.trade_status or current.volume <= 0:
        return "not_tradable"
    if current.is_st:
        return "st_stock"
    return None


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(FEATURE_QUANTUM, rounding=ROUND_HALF_EVEN)
