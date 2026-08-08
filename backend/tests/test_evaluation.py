from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_codex.backtest.evaluation import (
    EvaluationError,
    EvaluationPeriod,
    WalkForwardConfig,
    WalkForwardEvaluator,
)
from trading_codex.domain.contracts import MarketRegimeLabel


def _series(*, edge: Decimal, cost: Decimal) -> tuple[EvaluationPeriod, ...]:
    start = datetime(2024, 1, 1, 8, tzinfo=UTC)
    regimes = tuple(MarketRegimeLabel)
    return tuple(
        EvaluationPeriod(
            as_of=start + timedelta(days=index),
            gross_return=edge
            + (Decimal("0.001") if index % 2 == 0 else Decimal("-0.0005")),
            benchmark_return=Decimal("0.0005")
            + (Decimal("0.0003") if index % 3 == 0 else Decimal("-0.0001")),
            cost_rate=cost,
            regime=regimes[(index // 5) % len(regimes)],
        )
        for index in range(60)
    )


def test_walk_forward_report_is_oos_net_of_cost_and_contains_required_evidence() -> None:
    report = WalkForwardEvaluator(
        WalkForwardConfig(
            train_periods=20,
            test_periods=10,
            bootstrap_block_size=4,
            bootstrap_samples=200,
            random_seed=7,
        )
    ).evaluate(
        {
            "conservative": _series(edge=Decimal("0.0010"), cost=Decimal("0.0002")),
            "preferred": _series(edge=Decimal("0.0020"), cost=Decimal("0.0002")),
        }
    )

    assert len(report.folds) == 4
    assert all(fold.selected_parameter == "preferred" for fold in report.folds)
    assert all(fold.train_end < fold.test_start for fold in report.folds)
    assert report.out_of_sample.observations == 40
    assert report.out_of_sample.average_cost_rate == Decimal("0.000200000000")
    gross_growth = Decimal(1)
    for period in _series(edge=Decimal("0.0020"), cost=Decimal("0.0002"))[20:]:
        gross_growth *= Decimal(1) + period.gross_return
    assert report.out_of_sample.cumulative_return < gross_growth - Decimal(1)
    assert {item.regime for item in report.regime_slices} == set(MarketRegimeLabel)
    assert [item.parameter_id for item in report.parameter_sensitivity] == [
        "conservative",
        "preferred",
    ]
    assert (
        report.parameter_sensitivity[1].metrics.cumulative_return
        > report.parameter_sensitivity[0].metrics.cumulative_return
    )
    assert report.out_of_sample.max_drawdown >= 0
    assert report.block_bootstrap.block_size == 4
    assert report.block_bootstrap.samples == 200
    assert Decimal(0) <= report.block_bootstrap.probability_positive <= Decimal(1)
    assert report.deflated_sharpe.trials == 2
    assert report.deflated_sharpe.observations == 40
    assert Decimal(0) <= report.deflated_sharpe.probability <= Decimal(1)


def test_walk_forward_rejects_misaligned_or_naive_time_series() -> None:
    valid = _series(edge=Decimal("0.001"), cost=Decimal(0))
    shifted = tuple(
        EvaluationPeriod(
            as_of=period.as_of + timedelta(hours=1),
            gross_return=period.gross_return,
            benchmark_return=period.benchmark_return,
            cost_rate=period.cost_rate,
            regime=period.regime,
        )
        for period in valid
    )
    evaluator = WalkForwardEvaluator(
        WalkForwardConfig(train_periods=20, test_periods=10, bootstrap_samples=10)
    )

    with pytest.raises(EvaluationError, match="same as_of grid"):
        evaluator.evaluate({"a": valid, "b": shifted})

    with pytest.raises(EvaluationError, match="timezone-aware"):
        EvaluationPeriod(
            as_of=datetime(2024, 1, 1),
            gross_return=Decimal(0),
            benchmark_return=Decimal(0),
            cost_rate=Decimal(0),
            regime=MarketRegimeLabel.RISK_ON,
        )
