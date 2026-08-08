from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Context, Decimal, localcontext

import pytest

from trading_codex.domain.contracts import (
    AllocationState,
    Candidate,
    FeatureSet,
    FeatureVector,
    MarketRegimeAssessment,
    MarketRegimeLabel,
    RegimeFeatureVector,
    RegimeProbability,
    StrategyIntent,
    StrategyKind,
    StrategyProposal,
    TargetWeight,
)
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import (
    DailyBar,
    DecisionPoint,
    DecisionSnapshot,
    InstrumentRule,
    OpeningBar,
    SnapshotValidationError,
)
from trading_codex.domain.pipeline import DecisionPipeline
from trading_codex.portfolio.allocation import AllocationConfig
from trading_codex.portfolio.regime_allocation import (
    RegimeAllocationConfig,
    RegimeAwareAllocator,
)
from trading_codex.regime.features import MarketRegimeFeaturePipeline
from trading_codex.strategies.pool import StrategyPool


def _market_snapshot(
    *,
    decision_point: DecisionPoint = DecisionPoint.OPENING_0935,
    daily_drift: Decimal = Decimal("0.004"),
    opening_return: Decimal = Decimal("0.005"),
) -> DecisionSnapshot:
    start = date(2024, 1, 1)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(10))
    bars = []
    for code_index, code in enumerate(codes):
        previous_execution = None
        for index in range(26):
            day = start + timedelta(days=index)
            oscillation = Decimal((index + code_index) % 3 - 1) / Decimal(1_000)
            signal = Decimal(10 + code_index) * (
                Decimal(1) + daily_drift * Decimal(index) + oscillation
            )
            execution = signal + Decimal(5)
            bars.append(
                DailyBar(
                    code=code,
                    trade_date=day,
                    signal_close=signal,
                    execution_close=execution,
                    previous_close=previous_execution or execution,
                    volume=100_000,
                    trade_status=True,
                    is_st=False,
                    available_at=datetime(day.year, day.month, day.day, 7, tzinfo=UTC),
                    amount=Decimal(1_000_000 + code_index * 10_000),
                    turnover=Decimal("0.02") + Decimal(code_index) / Decimal(10_000),
                )
            )
            previous_execution = execution
    history_end = start + timedelta(days=25)
    decision_date = history_end + timedelta(days=1)
    opening_time = datetime(2024, 1, 27, 1, 35, tzinfo=UTC)
    previous_by_code = {
        bar.code: bar.execution_close for bar in bars if bar.trade_date == history_end
    }
    return DecisionSnapshot(
        as_of=opening_time,
        decision_date=decision_date,
        execution_deadline=datetime(2024, 1, 27, 8, tzinfo=UTC),
        cash=Decimal("1000000"),
        candidate_codes=codes,
        bars=tuple(sorted(bars, key=lambda bar: (bar.code, bar.trade_date))),
        positions=(),
        rules=tuple(
            InstrumentRule(code=code, lot_size=100, price_limit_ratio=Decimal("0.10"))
            for code in codes
        ),
        source_payloads=(canonical_sha256({"fixture": "market-regime"}),),
        decision_point=decision_point,
        regime_codes=codes,
        opening_bars=tuple(
            OpeningBar(
                code=code,
                timestamp=opening_time,
                open_price=Decimal(10 + code_index),
                close_price=Decimal(10 + code_index) * (Decimal(1) + opening_return),
                previous_close=previous_by_code[code],
                volume=10_000,
                amount=Decimal(100_000 + code_index * 1_000),
                trade_status=True,
                is_st=False,
                available_at=opening_time,
            )
            for code_index, code in enumerate(codes)
        ),
    )


def _feature_set(snapshot_id: str, as_of: datetime) -> FeatureSet:
    features = (
        FeatureVector(
            code="sh.600000",
            latest_trade_date=date(2024, 1, 26),
            momentum=Decimal("0.20"),
            annualized_volatility=Decimal("0.20"),
            risk_adjusted_momentum=Decimal("1.00"),
            short_term_return=Decimal("-0.05"),
            observations=26,
        ),
        FeatureVector(
            code="sh.600001",
            latest_trade_date=date(2024, 1, 26),
            momentum=Decimal("0.10"),
            annualized_volatility=Decimal("0.10"),
            risk_adjusted_momentum=Decimal("1.00"),
            short_term_return=Decimal("-0.10"),
            observations=26,
        ),
        FeatureVector(
            code="sh.600002",
            latest_trade_date=date(2024, 1, 26),
            momentum=Decimal("0.05"),
            annualized_volatility=Decimal("0.05"),
            risk_adjusted_momentum=Decimal("1.00"),
            short_term_return=Decimal("0.02"),
            observations=26,
        ),
    )
    candidates = tuple(
        Candidate(
            code=feature.code,
            rank=rank,
            momentum=feature.momentum,
            annualized_volatility=feature.annualized_volatility,
            score=feature.risk_adjusted_momentum,
        )
        for rank, feature in enumerate(features, start=1)
    )
    return FeatureSet(
        snapshot_id=snapshot_id,
        as_of=as_of,
        version="fixture-features-v1",
        features=features,
        candidates=candidates,
        exclusions=(),
    )


def _assessment(
    snapshot: DecisionSnapshot,
    *,
    selected: MarketRegimeLabel,
    selected_probability: Decimal,
    previous_probability: Decimal,
    emergency: bool = False,
) -> MarketRegimeAssessment:
    remaining = (Decimal(1) - selected_probability - previous_probability) / Decimal(2)
    values = {
        MarketRegimeLabel.RISK_ON: previous_probability,
        MarketRegimeLabel.MEAN_REVERTING: remaining,
        MarketRegimeLabel.DEFENSIVE: remaining,
        MarketRegimeLabel.RISK_OFF: remaining,
    }
    values[selected] = selected_probability
    return MarketRegimeAssessment(
        snapshot_id=snapshot.snapshot_id,
        as_of=snapshot.as_of,
        version="fixture-regime-v1",
        features=RegimeFeatureVector(
            trend_return=Decimal(0),
            annualized_volatility=Decimal("0.20"),
            breadth=Decimal("0.50"),
            average_turnover=Decimal("0.02"),
            concentration=Decimal("0.20"),
            opening_return=Decimal(0),
            universe_size=10,
            daily_coverage=Decimal(1),
            opening_coverage=Decimal(1),
        ),
        probabilities=tuple(
            RegimeProbability(label=label, probability=values[label], score=Decimal(0))
            for label in MarketRegimeLabel
        ),
        selected=selected,
        emergency_risk_off=emergency,
        explanations=("fixture",),
    )


def _proposals(snapshot: DecisionSnapshot) -> tuple[StrategyProposal, ...]:
    codes = snapshot.candidate_codes
    groups = {
        StrategyKind.MOMENTUM: codes[:5],
        StrategyKind.SHORT_TERM_REVERSAL: codes[2:7],
        StrategyKind.DEFENSIVE_LOW_VOLATILITY: codes[5:],
        StrategyKind.CASH: (),
    }
    return tuple(
        StrategyProposal(
            snapshot_id=snapshot.snapshot_id,
            strategy=strategy,
            version=f"fixture-{strategy.value}-v1",
            intents=tuple(
                StrategyIntent(
                    code=code,
                    rank=rank,
                    score=Decimal(1),
                    inverse_volatility=Decimal(1),
                )
                for rank, code in enumerate(groups[strategy], start=1)
            ),
        )
        for strategy in StrategyKind
    )


def test_regime_pipeline_exposes_six_features_and_normalized_probabilities() -> None:
    snapshot = _market_snapshot()

    assessment = MarketRegimeFeaturePipeline().compute(snapshot)

    assert assessment.selected is MarketRegimeLabel.RISK_ON
    assert assessment.features.trend_return > 0
    assert assessment.features.annualized_volatility >= 0
    assert assessment.features.breadth == Decimal(1)
    assert assessment.features.average_turnover > 0
    assert Decimal(0) < assessment.features.concentration < Decimal(1)
    assert assessment.features.opening_return == Decimal("0.005000000000")
    assert sum(
        (probability.probability for probability in assessment.probabilities), Decimal(0)
    ) == Decimal(1)
    assert {probability.label for probability in assessment.probabilities} == set(
        MarketRegimeLabel
    )
    assert len(assessment.explanations) >= 7


def test_regime_pipeline_rejects_wrong_opening_checkpoint() -> None:
    snapshot = _market_snapshot()
    shifted = tuple(
        replace(
            bar,
            timestamp=bar.timestamp + timedelta(minutes=5),
            available_at=bar.available_at + timedelta(minutes=5),
        )
        for bar in snapshot.opening_bars
    )

    with pytest.raises(SnapshotValidationError, match="09:35"):
        MarketRegimeFeaturePipeline().compute(
            replace(
                snapshot,
                as_of=snapshot.as_of + timedelta(minutes=10),
                opening_bars=shifted,
            )
        )


def test_strategy_pool_contains_momentum_reversal_low_volatility_and_cash() -> None:
    snapshot = _market_snapshot()
    features = _feature_set(snapshot.snapshot_id, snapshot.as_of)
    proposals = StrategyPool().propose(features)
    by_strategy = {proposal.strategy: proposal for proposal in proposals}

    assert set(by_strategy) == set(StrategyKind)
    assert by_strategy[StrategyKind.MOMENTUM].intents[0].code == "sh.600000"
    assert by_strategy[StrategyKind.SHORT_TERM_REVERSAL].intents[0].code == "sh.600001"
    assert by_strategy[StrategyKind.DEFENSIVE_LOW_VOLATILITY].intents[0].code == "sh.600002"
    assert by_strategy[StrategyKind.CASH].intents == ()
    with localcontext(Context(prec=10)):
        low_precision = StrategyPool().propose(features)
    with localcontext(Context(prec=50)):
        high_precision = StrategyPool().propose(features)
    assert low_precision == high_precision


def test_allocator_enforces_hysteresis_checkpoint_turnover_and_emergency() -> None:
    opening = _market_snapshot()
    previous = AllocationState(
        as_of=opening.as_of - timedelta(days=1),
        active_strategy=StrategyKind.MOMENTUM,
        weights=tuple(
            TargetWeight(code=code, weight=Decimal("0.19"), rank=rank)
            for rank, code in enumerate(opening.candidate_codes[:5], start=1)
        ),
        cash_weight=Decimal("0.05"),
    )
    allocator = RegimeAwareAllocator(
        RegimeAllocationConfig(
            base=AllocationConfig(),
            switch_hysteresis=Decimal("0.08"),
            max_turnover=Decimal("0.20"),
        )
    )

    weak = allocator.allocate(
        opening,
        _assessment(
            opening,
            selected=MarketRegimeLabel.DEFENSIVE,
            selected_probability=Decimal("0.40"),
            previous_probability=Decimal("0.35"),
        ),
        _proposals(opening),
        previous=previous,
    )
    assert weak.active_strategy is StrategyKind.MOMENTUM

    eod = replace(opening, decision_point=DecisionPoint.EOD)
    held = allocator.allocate(
        eod,
        _assessment(
            eod,
            selected=MarketRegimeLabel.DEFENSIVE,
            selected_probability=Decimal("0.60"),
            previous_probability=Decimal("0.20"),
        ),
        _proposals(eod),
        previous=previous,
    )
    assert held.active_strategy is StrategyKind.MOMENTUM

    switched = allocator.allocate(
        opening,
        _assessment(
            opening,
            selected=MarketRegimeLabel.DEFENSIVE,
            selected_probability=Decimal("0.60"),
            previous_probability=Decimal("0.20"),
        ),
        _proposals(opening),
        previous=previous,
    )
    assert switched.active_strategy is StrategyKind.DEFENSIVE_LOW_VOLATILITY
    assert switched.turnover <= Decimal("0.20")
    assert {weight.code for weight in switched.weights} & set(opening.candidate_codes[:5])
    assert {weight.code for weight in switched.weights} & set(opening.candidate_codes[5:])

    frozen = RegimeAwareAllocator(
        RegimeAllocationConfig(max_turnover=Decimal(0))
    ).allocate(
        opening,
        _assessment(
            opening,
            selected=MarketRegimeLabel.DEFENSIVE,
            selected_probability=Decimal("0.60"),
            previous_probability=Decimal("0.20"),
        ),
        _proposals(opening),
        previous=previous,
    )
    assert frozen.weights == previous.weights
    assert frozen.turnover == Decimal(0)

    emergency = allocator.allocate(
        eod,
        _assessment(
            eod,
            selected=MarketRegimeLabel.RISK_OFF,
            selected_probability=Decimal("0.70"),
            previous_probability=Decimal("0.10"),
            emergency=True,
        ),
        _proposals(eod),
        previous=previous,
    )
    assert emergency.active_strategy is StrategyKind.MOMENTUM
    assert emergency.weights == ()
    assert emergency.cash_weight == Decimal(1)
    assert emergency.turnover == Decimal("0.95")
    assert emergency.emergency_risk_off is True

    emergency_at_checkpoint = allocator.allocate(
        opening,
        _assessment(
            opening,
            selected=MarketRegimeLabel.RISK_OFF,
            selected_probability=Decimal("0.70"),
            previous_probability=Decimal("0.10"),
            emergency=True,
        ),
        _proposals(opening),
        previous=previous,
    )
    assert emergency_at_checkpoint.active_strategy is StrategyKind.CASH


def test_decision_run_records_regime_strategy_pool_and_allocator_versions() -> None:
    snapshot = _market_snapshot()
    run = DecisionPipeline().run(snapshot)

    assert run.regime.version == "interpretable-market-regime-v1"
    assert {proposal.strategy for proposal in run.strategy_proposals} == set(StrategyKind)
    assert run.proposal.strategy is run.allocated.active_strategy
    assert run.allocator_version == run.allocated.version
    assert run.allocated.decision_point is DecisionPoint.OPENING_0935
    assert all(
        feature.latest_trade_date < snapshot.decision_date
        for feature in run.features.features
    )
    assert run.execution.orders
    assert all(
        order.reference_price == snapshot.opening_bar_for(order.code).close_price
        for order in run.execution.orders
        if snapshot.opening_bar_for(order.code) is not None
    )
