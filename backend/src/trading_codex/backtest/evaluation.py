import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from statistics import NormalDist

from trading_codex.domain.contracts import MarketRegimeLabel

EVALUATION_VERSION = "walk-forward-regime-evaluation-v1"
METRIC_QUANTUM = Decimal("0.000000000001")


class EvaluationError(ValueError):
    """An evaluation input would make out-of-sample evidence ambiguous."""


@dataclass(frozen=True)
class EvaluationPeriod:
    as_of: datetime
    gross_return: Decimal
    benchmark_return: Decimal
    cost_rate: Decimal
    regime: MarketRegimeLabel

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise EvaluationError("evaluation as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        if not isinstance(self.regime, MarketRegimeLabel):
            raise EvaluationError("evaluation regime is invalid")
        if not all(
            value.is_finite()
            for value in (self.gross_return, self.benchmark_return, self.cost_rate)
        ):
            raise EvaluationError("evaluation returns and costs must be finite")
        if self.gross_return <= Decimal(-1) or self.benchmark_return <= Decimal(-1):
            raise EvaluationError("period returns must be greater than -1")
        if self.cost_rate < 0:
            raise EvaluationError("cost rate must be non-negative")
        if self.net_return <= Decimal(-1):
            raise EvaluationError("net return must be greater than -1")

    @property
    def net_return(self) -> Decimal:
        return self.gross_return - self.cost_rate


@dataclass(frozen=True)
class PerformanceMetrics:
    observations: int
    cumulative_return: Decimal
    annualized_return: Decimal
    annualized_volatility: Decimal
    sharpe: Decimal
    max_drawdown: Decimal
    annualized_alpha: Decimal
    beta: Decimal
    average_cost_rate: Decimal


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    selected_parameter: str
    training_sharpe: Decimal


@dataclass(frozen=True)
class RegimeSlice:
    regime: MarketRegimeLabel
    metrics: PerformanceMetrics


@dataclass(frozen=True)
class ParameterSensitivity:
    parameter_id: str
    metrics: PerformanceMetrics


@dataclass(frozen=True)
class BlockBootstrapEvidence:
    block_size: int
    samples: int
    mean_active_return: Decimal
    confidence_lower: Decimal
    confidence_upper: Decimal
    probability_positive: Decimal


@dataclass(frozen=True)
class DeflatedSharpeEvidence:
    observed_periodic_sharpe: Decimal
    expected_max_sharpe: Decimal
    trials: int
    observations: int
    probability: Decimal


@dataclass(frozen=True)
class WalkForwardReport:
    version: str
    folds: tuple[WalkForwardFold, ...]
    out_of_sample: PerformanceMetrics
    regime_slices: tuple[RegimeSlice, ...]
    parameter_sensitivity: tuple[ParameterSensitivity, ...]
    block_bootstrap: BlockBootstrapEvidence
    deflated_sharpe: DeflatedSharpeEvidence


@dataclass(frozen=True)
class WalkForwardConfig:
    train_periods: int = 252
    test_periods: int = 63
    annualization_periods: int = 252
    bootstrap_block_size: int = 5
    bootstrap_samples: int = 1_000
    bootstrap_confidence: Decimal = Decimal("0.95")
    random_seed: int = 20260808
    version: str = EVALUATION_VERSION

    def __post_init__(self) -> None:
        if self.train_periods < 2 or self.test_periods < 1:
            raise ValueError("walk-forward train/test periods are too short")
        if self.annualization_periods < 1:
            raise ValueError("annualization periods must be positive")
        if self.bootstrap_block_size < 1 or self.bootstrap_samples < 1:
            raise ValueError("bootstrap configuration must be positive")
        if not Decimal(0) < self.bootstrap_confidence < Decimal(1):
            raise ValueError("bootstrap confidence must be in (0, 1)")
        if not self.version:
            raise ValueError("evaluation version is required")


class WalkForwardEvaluator:
    def __init__(self, config: WalkForwardConfig | None = None) -> None:
        self.config = config or WalkForwardConfig()

    def evaluate(
        self,
        parameter_series: Mapping[str, Iterable[EvaluationPeriod]],
    ) -> WalkForwardReport:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            series = self._validated_series(parameter_series)
            reference = next(iter(series.values()))
            folds: list[WalkForwardFold] = []
            selected_periods: list[EvaluationPeriod] = []
            test_indices: list[int] = []

            test_start = self.config.train_periods
            while test_start + self.config.test_periods <= len(reference):
                train_start = test_start - self.config.train_periods
                test_end = test_start + self.config.test_periods
                training_metrics = {
                    parameter_id: _performance(
                        periods[train_start:test_start],
                        annualization=self.config.annualization_periods,
                    )
                    for parameter_id, periods in series.items()
                }
                selected_parameter = max(
                    sorted(training_metrics),
                    key=lambda parameter_id: training_metrics[parameter_id].sharpe,
                )
                selected_periods.extend(series[selected_parameter][test_start:test_end])
                test_indices.extend(range(test_start, test_end))
                folds.append(
                    WalkForwardFold(
                        train_start=reference[train_start].as_of,
                        train_end=reference[test_start - 1].as_of,
                        test_start=reference[test_start].as_of,
                        test_end=reference[test_end - 1].as_of,
                        selected_parameter=selected_parameter,
                        training_sharpe=training_metrics[selected_parameter].sharpe,
                    )
                )
                test_start = test_end

            if not folds:
                raise EvaluationError("not enough periods for one complete walk-forward fold")

            out_of_sample = tuple(selected_periods)
            slices = tuple(
                RegimeSlice(
                    regime=regime,
                    metrics=_performance(
                        tuple(period for period in out_of_sample if period.regime is regime),
                        annualization=self.config.annualization_periods,
                    ),
                )
                for regime in MarketRegimeLabel
                if any(period.regime is regime for period in out_of_sample)
            )
            sensitivity = tuple(
                ParameterSensitivity(
                    parameter_id=parameter_id,
                    metrics=_performance(
                        tuple(periods[index] for index in test_indices),
                        annualization=self.config.annualization_periods,
                    ),
                )
                for parameter_id, periods in sorted(series.items())
            )
            return WalkForwardReport(
                version=self.config.version,
                folds=tuple(folds),
                out_of_sample=_performance(
                    out_of_sample,
                    annualization=self.config.annualization_periods,
                ),
                regime_slices=slices,
                parameter_sensitivity=sensitivity,
                block_bootstrap=self._bootstrap(out_of_sample),
                deflated_sharpe=_deflated_sharpe(
                    out_of_sample,
                    trials=len(series),
                ),
            )

    def _validated_series(
        self,
        parameter_series: Mapping[str, Iterable[EvaluationPeriod]],
    ) -> dict[str, tuple[EvaluationPeriod, ...]]:
        if not parameter_series:
            raise EvaluationError("at least one parameter series is required")
        if any(
            not isinstance(parameter_id, str) or not parameter_id.strip()
            for parameter_id in parameter_series
        ):
            raise EvaluationError("parameter ids must be non-empty")
        series = {
            parameter_id: tuple(periods)
            for parameter_id, periods in sorted(parameter_series.items())
        }
        reference: tuple[EvaluationPeriod, ...] | None = None
        for parameter_id, periods in series.items():
            as_ofs = tuple(period.as_of for period in periods)
            if as_ofs != tuple(sorted(set(as_ofs))):
                raise EvaluationError(
                    f"parameter series {parameter_id} must have strictly increasing as_of values"
                )
            if reference is None:
                reference = periods
                continue
            if as_ofs != tuple(period.as_of for period in reference):
                raise EvaluationError("parameter series must share the same as_of grid")
            for current, expected in zip(periods, reference):
                if (
                    current.benchmark_return != expected.benchmark_return
                    or current.regime is not expected.regime
                ):
                    raise EvaluationError(
                        "parameter series disagree on benchmark or regime labels"
                    )
        assert reference is not None
        return series

    def _bootstrap(
        self,
        periods: tuple[EvaluationPeriod, ...],
    ) -> BlockBootstrapEvidence:
        active = tuple(period.net_return - period.benchmark_return for period in periods)
        block_size = min(self.config.bootstrap_block_size, len(active))
        random_source = random.Random(self.config.random_seed)
        means = []
        for _ in range(self.config.bootstrap_samples):
            sample = []
            while len(sample) < len(active):
                start = random_source.randrange(len(active) - block_size + 1)
                sample.extend(active[start : start + block_size])
            sample = sample[: len(active)]
            means.append(sum(sample, Decimal(0)) / Decimal(len(sample)))
        ordered = sorted(means)
        tail = (Decimal(1) - self.config.bootstrap_confidence) / Decimal(2)
        lower = ordered[_quantile_index(tail, len(ordered))]
        upper = ordered[_quantile_index(Decimal(1) - tail, len(ordered))]
        return BlockBootstrapEvidence(
            block_size=block_size,
            samples=self.config.bootstrap_samples,
            mean_active_return=_quantize(sum(active, Decimal(0)) / Decimal(len(active))),
            confidence_lower=_quantize(lower),
            confidence_upper=_quantize(upper),
            probability_positive=_quantize(
                Decimal(sum(value > 0 for value in means)) / Decimal(len(means))
            ),
        )


def _performance(
    periods: tuple[EvaluationPeriod, ...],
    *,
    annualization: int,
) -> PerformanceMetrics:
    if not periods:
        raise EvaluationError("performance slice must contain at least one period")
    returns = tuple(period.net_return for period in periods)
    benchmarks = tuple(period.benchmark_return for period in periods)
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = _sample_variance(returns, mean)
    standard_deviation = variance.sqrt()
    annualized_volatility = standard_deviation * Decimal(annualization).sqrt()
    sharpe = (
        mean / standard_deviation * Decimal(annualization).sqrt()
        if standard_deviation > 0
        else Decimal(0)
    )

    growth = Decimal(1)
    peak = Decimal(1)
    max_drawdown = Decimal(0)
    for period_return in returns:
        growth *= Decimal(1) + period_return
        peak = max(peak, growth)
        max_drawdown = max(max_drawdown, Decimal(1) - growth / peak)
    annualized_return = (
        (growth.ln() * Decimal(annualization) / Decimal(len(returns))).exp() - Decimal(1)
        if growth > 0
        else Decimal(-1)
    )

    benchmark_mean = sum(benchmarks, Decimal(0)) / Decimal(len(benchmarks))
    benchmark_variance = _sample_variance(benchmarks, benchmark_mean)
    covariance = (
        sum(
            (value - mean) * (benchmark - benchmark_mean)
            for value, benchmark in zip(returns, benchmarks)
        )
        / Decimal(len(returns) - 1)
        if len(returns) > 1
        else Decimal(0)
    )
    beta = covariance / benchmark_variance if benchmark_variance > 0 else Decimal(0)
    annualized_alpha = (mean - beta * benchmark_mean) * Decimal(annualization)
    return PerformanceMetrics(
        observations=len(periods),
        cumulative_return=_quantize(growth - Decimal(1)),
        annualized_return=_quantize(annualized_return),
        annualized_volatility=_quantize(annualized_volatility),
        sharpe=_quantize(sharpe),
        max_drawdown=_quantize(max_drawdown),
        annualized_alpha=_quantize(annualized_alpha),
        beta=_quantize(beta),
        average_cost_rate=_quantize(
            sum((period.cost_rate for period in periods), Decimal(0))
            / Decimal(len(periods))
        ),
    )


def _deflated_sharpe(
    periods: tuple[EvaluationPeriod, ...],
    *,
    trials: int,
) -> DeflatedSharpeEvidence:
    returns = tuple(period.net_return for period in periods)
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = _sample_variance(returns, mean)
    standard_deviation = variance.sqrt()
    observed = mean / standard_deviation if standard_deviation > 0 else Decimal(0)
    observations = len(returns)

    expected_max = Decimal(0)
    if trials > 1 and observations > 1:
        variance_of_sharpe = (Decimal(1) + observed**2 / Decimal(2)) / Decimal(
            observations
        )
        normal = NormalDist()
        euler_gamma = Decimal("0.5772156649015329")
        first = Decimal(str(normal.inv_cdf(1 - 1 / trials)))
        second = Decimal(str(normal.inv_cdf(1 - 1 / (trials * 2.718281828459045))))
        expected_max = variance_of_sharpe.sqrt() * (
            (Decimal(1) - euler_gamma) * first + euler_gamma * second
        )

    probability = Decimal("0.5")
    if observations > 2 and standard_deviation > 0:
        centered = tuple(value - mean for value in returns)
        population_variance = sum((value**2 for value in centered), Decimal(0)) / Decimal(
            observations
        )
        population_std = population_variance.sqrt()
        skewness = (
            sum((value**3 for value in centered), Decimal(0))
            / Decimal(observations)
            / population_std**3
        )
        kurtosis = (
            sum((value**4 for value in centered), Decimal(0))
            / Decimal(observations)
            / population_std**4
        )
        denominator_squared = (
            Decimal(1)
            - skewness * observed
            + (kurtosis - Decimal(1)) * observed**2 / Decimal(4)
        )
        if denominator_squared > 0:
            statistic = (
                (observed - expected_max)
                * Decimal(observations - 1).sqrt()
                / denominator_squared.sqrt()
            )
            probability = Decimal(str(NormalDist().cdf(float(statistic))))
    return DeflatedSharpeEvidence(
        observed_periodic_sharpe=_quantize(observed),
        expected_max_sharpe=_quantize(expected_max),
        trials=trials,
        observations=observations,
        probability=_quantize(probability),
    )


def _sample_variance(values: tuple[Decimal, ...], mean: Decimal) -> Decimal:
    if len(values) < 2:
        return Decimal(0)
    return sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(
        len(values) - 1
    )


def _quantile_index(quantile: Decimal, size: int) -> int:
    return min(size - 1, max(0, int(quantile * Decimal(size - 1))))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(METRIC_QUANTUM, rounding=ROUND_HALF_EVEN)
