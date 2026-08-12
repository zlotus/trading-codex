from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from trading_codex.domain.contracts import (
    MarketRegimeAssessment,
    MarketRegimeLabel,
    RegimeFeatureVector,
    RegimeProbability,
)
from trading_codex.domain.models import SHANGHAI, DecisionSnapshot, SnapshotValidationError

REGIME_VERSION = "interpretable-market-regime-v1"
EOD_REGIME_VERSION = "interpretable-market-regime-eod-v1"
REGIME_QUANTUM = Decimal("0.000000000001")
PROBABILITY_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class MarketRegimeConfig:
    trend_lookback: int = 20
    volatility_lookback: int = 20
    annualization_days: int = 252
    minimum_universe_size: int = 5
    minimum_daily_coverage: Decimal = Decimal("0.80")
    minimum_opening_coverage: Decimal = Decimal("0.80")
    concentration_fraction: Decimal = Decimal("0.10")
    trend_scale: Decimal = Decimal("0.08")
    volatility_center: Decimal = Decimal("0.20")
    volatility_scale: Decimal = Decimal("0.15")
    turnover_center: Decimal = Decimal("1.00")
    turnover_scale: Decimal = Decimal("1.00")
    concentration_center: Decimal = Decimal("0.20")
    concentration_scale: Decimal = Decimal("0.20")
    opening_scale: Decimal = Decimal("0.015")
    opening_checkpoint_minutes: int = 9 * 60 + 35
    softmax_temperature: Decimal = Decimal("0.75")
    emergency_volatility: Decimal = Decimal("0.45")
    emergency_trend: Decimal = Decimal("-0.12")
    emergency_breadth: Decimal = Decimal("0.20")
    emergency_opening_return: Decimal = Decimal("-0.05")
    opening_feature_enabled: bool = True
    version: str = REGIME_VERSION

    def __post_init__(self) -> None:
        if self.trend_lookback < 1 or self.volatility_lookback < 2:
            raise ValueError("regime lookbacks are too short")
        if self.annualization_days < 1 or self.minimum_universe_size < 1:
            raise ValueError("regime sample requirements must be positive")
        for name in ("minimum_daily_coverage", "minimum_opening_coverage"):
            value = getattr(self, name)
            if not Decimal(0) < value <= Decimal(1):
                raise ValueError(f"{name} must be in (0, 1]")
        if not Decimal(0) < self.concentration_fraction <= Decimal(1):
            raise ValueError("concentration fraction must be in (0, 1]")
        for name in (
            "trend_scale",
            "volatility_scale",
            "turnover_scale",
            "concentration_scale",
            "opening_scale",
            "softmax_temperature",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not self.version:
            raise ValueError("regime version is required")
        if not 0 <= self.opening_checkpoint_minutes < 24 * 60:
            raise ValueError("opening checkpoint minutes must describe one day")


class MarketRegimeFeaturePipeline:
    def __init__(self, config: MarketRegimeConfig | None = None) -> None:
        self.config = config or MarketRegimeConfig()

    @property
    def version(self) -> str:
        return self.config.version

    def compute(self, snapshot: DecisionSnapshot) -> MarketRegimeAssessment:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            features = self._features(snapshot)
            scores = self._scores(features)
            probabilities = self._probabilities(scores)
            emergency, emergency_reasons = self._emergency(features)
            selected = (
                MarketRegimeLabel.RISK_OFF
                if emergency
                else max(probabilities, key=lambda item: (item.probability, item.label.value)).label
            )
            probability = next(
                item.probability for item in probabilities if item.label is selected
            )
            explanations = (
                f"trend_return={features.trend_return}",
                f"annualized_volatility={features.annualized_volatility}",
                f"breadth={features.breadth}",
                f"average_turnover={features.average_turnover}",
                f"concentration={features.concentration}",
                "opening_feature="
                + ("enabled" if self.config.opening_feature_enabled else "disabled_eod"),
                f"opening_return={features.opening_return}",
                f"selected={selected.value}; probability={probability}",
                *emergency_reasons,
            )
            return MarketRegimeAssessment(
                snapshot_id=snapshot.snapshot_id,
                as_of=snapshot.as_of,
                version=self.version,
                features=features,
                probabilities=probabilities,
                selected=selected,
                emergency_risk_off=emergency,
                explanations=explanations,
            )

    def _features(self, snapshot: DecisionSnapshot) -> RegimeFeatureVector:
        codes = snapshot.regime_codes
        if len(codes) < self.config.minimum_universe_size:
            raise SnapshotValidationError(
                "regime universe is smaller than the configured minimum"
            )

        required = max(self.config.trend_lookback, self.config.volatility_lookback) + 1
        price_series: dict[str, tuple[Decimal, ...]] = {}
        trend_returns: list[Decimal] = []
        current_bars = []
        for code in codes:
            code_bars = snapshot.bars_for(code)
            priced = tuple(
                bar
                for bar in code_bars
                if bar.trade_status and bar.signal_close is not None
            )
            if len(priced) < required:
                continue
            prices = tuple(bar.signal_close for bar in priced[-required:])
            assert all(price is not None for price in prices)
            numeric_prices = tuple(price for price in prices if price is not None)
            price_series[code] = numeric_prices
            trend_returns.append(
                numeric_prices[-1] / numeric_prices[-self.config.trend_lookback - 1] - 1
            )
            current = code_bars[-1] if code_bars else None
            if (
                current is None
                or not current.trade_status
                or current.signal_close is None
                or current.amount is None
                or current.turnover is None
            ):
                price_series.pop(code, None)
                trend_returns.pop()
                continue
            current_bars.append(current)

        daily_coverage = Decimal(len(price_series)) / Decimal(len(codes))
        if daily_coverage < self.config.minimum_daily_coverage:
            raise SnapshotValidationError(
                "regime daily feature coverage is below the configured minimum"
            )

        market_returns: list[Decimal] = []
        for offset in range(self.config.volatility_lookback, 0, -1):
            returns = [
                prices[-offset] / prices[-offset - 1] - 1
                for prices in price_series.values()
            ]
            market_returns.append(sum(returns, Decimal(0)) / Decimal(len(returns)))
        mean_return = sum(market_returns, Decimal(0)) / Decimal(len(market_returns))
        variance = sum(
            (value - mean_return) ** 2 for value in market_returns
        ) / Decimal(len(market_returns) - 1)
        volatility = variance.sqrt() * Decimal(self.config.annualization_days).sqrt()

        current_amounts = sorted((bar.amount for bar in current_bars), reverse=True)
        assert all(amount is not None for amount in current_amounts)
        amounts = [amount for amount in current_amounts if amount is not None]
        total_amount = sum(amounts, Decimal(0))
        if total_amount <= 0:
            raise SnapshotValidationError("regime concentration requires positive traded amount")
        top_count = max(
            1,
            _ceiling(Decimal(len(amounts)) * self.config.concentration_fraction),
        )

        opening_coverage = Decimal(0)
        opening_return = Decimal(0)
        if self.config.opening_feature_enabled:
            opening = tuple(
                bar
                for code in price_series
                if (bar := snapshot.opening_bar_for(code)) is not None
                and bar.trade_status
                and bar.volume > 0
                and bar.amount > 0
                and (
                    bar.timestamp.astimezone(SHANGHAI).hour * 60
                    + bar.timestamp.astimezone(SHANGHAI).minute
                    == self.config.opening_checkpoint_minutes
                )
            )
            opening_coverage = Decimal(len(opening)) / Decimal(len(price_series))
            if opening_coverage < self.config.minimum_opening_coverage:
                raise SnapshotValidationError(
                    "regime opening feature coverage is below the configured minimum"
                )
            opening_amount = sum((bar.amount for bar in opening), Decimal(0))
            opening_return = sum(
                (bar.close_price / bar.open_price - 1) * bar.amount for bar in opening
            ) / opening_amount

        return RegimeFeatureVector(
            trend_return=_quantize(
                sum(trend_returns, Decimal(0)) / Decimal(len(trend_returns))
            ),
            annualized_volatility=_quantize(volatility),
            breadth=_quantize(
                Decimal(sum(value > 0 for value in trend_returns))
                / Decimal(len(trend_returns))
            ),
            average_turnover=_quantize(
                sum((bar.turnover for bar in current_bars if bar.turnover is not None), Decimal(0))
                / Decimal(len(current_bars))
            ),
            concentration=_quantize(sum(amounts[:top_count], Decimal(0)) / total_amount),
            opening_return=_quantize(opening_return),
            universe_size=len(codes),
            daily_coverage=_quantize(daily_coverage),
            opening_coverage=_quantize(opening_coverage),
        )

    def _scores(
        self, features: RegimeFeatureVector
    ) -> dict[MarketRegimeLabel, Decimal]:
        trend = _clamp(features.trend_return / self.config.trend_scale)
        volatility = _clamp(
            (features.annualized_volatility - self.config.volatility_center)
            / self.config.volatility_scale
        )
        breadth = _clamp((features.breadth - Decimal("0.50")) / Decimal("0.25"))
        turnover = _clamp(
            (features.average_turnover - self.config.turnover_center)
            / self.config.turnover_scale
        )
        concentration = _clamp(
            (features.concentration - self.config.concentration_center)
            / self.config.concentration_scale
        )
        opening = _clamp(features.opening_return / self.config.opening_scale)
        neutral_trend = Decimal(1) - min(abs(trend), Decimal(1))
        neutral_breadth = Decimal(1) - min(abs(breadth), Decimal(1))
        return {
            MarketRegimeLabel.RISK_ON: _quantize(
                Decimal("0.35") * trend
                + Decimal("0.25") * breadth
                + Decimal("0.15") * opening
                + Decimal("0.10") * turnover
                - Decimal("0.10") * volatility
                - Decimal("0.05") * concentration
            ),
            MarketRegimeLabel.MEAN_REVERTING: _quantize(
                Decimal("0.35") * neutral_trend
                + Decimal("0.20") * neutral_breadth
                + Decimal("0.20") * turnover
                - Decimal("0.15") * volatility
                - Decimal("0.10") * abs(opening)
            ),
            MarketRegimeLabel.DEFENSIVE: _quantize(
                -Decimal("0.25") * trend
                - Decimal("0.20") * breadth
                + Decimal("0.25") * volatility
                + Decimal("0.20") * concentration
                - Decimal("0.10") * opening
            ),
            MarketRegimeLabel.RISK_OFF: _quantize(
                -Decimal("0.35") * trend
                - Decimal("0.20") * breadth
                + Decimal("0.25") * volatility
                + Decimal("0.10") * concentration
                - Decimal("0.10") * opening
            ),
        }

    def _probabilities(
        self, scores: dict[MarketRegimeLabel, Decimal]
    ) -> tuple[RegimeProbability, ...]:
        exponentials = {
            label: (score / self.config.softmax_temperature).exp()
            for label, score in scores.items()
        }
        total = sum(exponentials.values(), Decimal(0))
        labels = tuple(MarketRegimeLabel)
        probabilities: list[RegimeProbability] = []
        assigned = Decimal(0)
        for label in labels[:-1]:
            probability = (exponentials[label] / total).quantize(
                PROBABILITY_QUANTUM, rounding=ROUND_HALF_EVEN
            )
            assigned += probability
            probabilities.append(
                RegimeProbability(label=label, probability=probability, score=scores[label])
            )
        last = labels[-1]
        probabilities.append(
            RegimeProbability(
                label=last,
                probability=Decimal(1) - assigned,
                score=scores[last],
            )
        )
        return tuple(probabilities)

    def _emergency(
        self, features: RegimeFeatureVector
    ) -> tuple[bool, tuple[str, ...]]:
        reasons = []
        if features.annualized_volatility >= self.config.emergency_volatility:
            reasons.append("emergency=volatility")
        if (
            features.trend_return <= self.config.emergency_trend
            and features.breadth <= self.config.emergency_breadth
        ):
            reasons.append("emergency=trend_and_breadth")
        if (
            self.config.opening_feature_enabled
            and features.opening_return <= self.config.emergency_opening_return
        ):
            reasons.append("emergency=opening_selloff")
        return bool(reasons), tuple(reasons)


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal(-2), min(Decimal(2), value))


def _ceiling(value: Decimal) -> int:
    integer = int(value)
    return integer if value == integer else integer + 1


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(REGIME_QUANTUM, rounding=ROUND_HALF_EVEN)
