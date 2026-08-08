from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Context, Decimal, localcontext

import pytest

from trading_codex.backtest.replay import HistoricalReplay
from trading_codex.domain.contracts import TargetPortfolio, TargetWeight
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import (
    DailyBar,
    DecisionSnapshot,
    InstrumentRule,
    PortfolioPosition,
    RiskValidationError,
    SnapshotValidationError,
    StaleMarketDataError,
)
from trading_codex.domain.pipeline import DecisionPipeline
from trading_codex.portfolio.execution import ExecutionPlanner
from trading_codex.risk.engine import HardRiskEngine, RiskConfig

PAYLOAD = canonical_sha256({"fixture": "decision-kernel"})


def _trending_snapshot(*, start_index: int, end_index: int) -> DecisionSnapshot:
    start = date(2024, 1, 1)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(10))
    bars: list[DailyBar] = []
    for code_index, code in enumerate(codes):
        previous_execution: Decimal | None = None
        for index in range(start_index, end_index + 1):
            day = start + timedelta(days=index)
            signal = (
                Decimal(40 + code_index * 5)
                + Decimal(index * (code_index + 1)) / Decimal(5)
                + Decimal(index % (code_index + 2)) / Decimal(100)
            )
            execution = signal + Decimal("10")
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
                )
            )
            previous_execution = execution
    decision_date = start + timedelta(days=end_index)
    return DecisionSnapshot(
        as_of=datetime(
            decision_date.year,
            decision_date.month,
            decision_date.day,
            8,
            tzinfo=UTC,
        ),
        decision_date=decision_date,
        execution_deadline=datetime(
            decision_date.year,
            decision_date.month,
            decision_date.day,
            8,
            tzinfo=UTC,
        )
        + timedelta(days=1),
        cash=Decimal("1000000"),
        candidate_codes=codes,
        bars=tuple(sorted(bars, key=lambda bar: (bar.code, bar.trade_date))),
        positions=(),
        rules=tuple(
            InstrumentRule(code=code, lot_size=100, price_limit_ratio=Decimal("0.10"))
            for code in codes
        ),
        source_payloads=(canonical_sha256({"start": start_index, "end": end_index}),),
    )


@pytest.mark.parametrize("end_index", [25, 30, 35])
def test_full_and_truncated_history_produce_identical_signals(end_index: int) -> None:
    pipeline = DecisionPipeline()
    full = pipeline.run(_trending_snapshot(start_index=0, end_index=end_index))
    truncated = pipeline.run(
        _trending_snapshot(start_index=end_index - 20, end_index=end_index)
    )

    assert full.features.candidates == truncated.features.candidates
    assert full.proposal.intents == truncated.proposal.intents
    assert full.allocated.weights == truncated.allocated.weights
    assert full.base_targets == truncated.base_targets
    assert len(full.base_targets) == 8


def test_unchanged_snapshot_replays_to_same_decision_and_base_target() -> None:
    pipeline = DecisionPipeline()
    snapshot = _trending_snapshot(start_index=0, end_index=30)

    first = pipeline.run(snapshot)
    second = pipeline.run(snapshot)
    replay = HistoricalReplay(pipeline).run((snapshot,))

    assert first.decision_id == second.decision_id
    assert first.base_targets == second.base_targets
    assert replay.configuration_id == pipeline.configuration_id
    assert replay.runs == (first,)


def test_replay_identity_does_not_depend_on_process_decimal_context() -> None:
    snapshot = _trending_snapshot(start_index=0, end_index=30)

    with localcontext(Context(prec=10)):
        low_precision = DecisionPipeline().run(snapshot)
    with localcontext(Context(prec=50)):
        high_precision = DecisionPipeline().run(snapshot)

    assert low_precision.decision_id == high_precision.decision_id
    assert low_precision == high_precision


def test_snapshot_rejects_future_rows_and_pipeline_rejects_stale_data() -> None:
    snapshot = _trending_snapshot(start_index=10, end_index=30)
    current = snapshot.state_on(snapshot.candidate_codes[0], snapshot.decision_date)
    assert current is not None
    future = replace(current, available_at=snapshot.as_of + timedelta(seconds=1))
    bars = tuple(future if bar == current else bar for bar in snapshot.bars)

    with pytest.raises(SnapshotValidationError, match="unavailable at as_of"):
        replace(snapshot, bars=bars)

    stale_as_of = snapshot.as_of + timedelta(days=8)
    stale = replace(
        snapshot,
        as_of=stale_as_of,
        execution_deadline=stale_as_of + timedelta(hours=1),
    )
    with pytest.raises(StaleMarketDataError):
        DecisionPipeline().run(stale)


def _constraint_snapshot() -> DecisionSnapshot:
    decision_date = date(2024, 2, 1)
    specs = {
        "sh.600001": (Decimal("11"), Decimal("10"), 100_000, True, False),
        "sh.600002": (Decimal("10"), Decimal("10"), 0, False, False),
        "sh.600003": (Decimal("10.50"), Decimal("10"), 100_000, True, True),
        "sh.600004": (Decimal("10"), Decimal("10"), 100_000, True, False),
        "sh.600005": (Decimal("9"), Decimal("10"), 100_000, True, False),
        "sh.600006": (Decimal("10"), Decimal("10"), 100_000, True, False),
    }
    bars = tuple(
        DailyBar(
            code=code,
            trade_date=decision_date,
            signal_close=price,
            execution_close=price,
            previous_close=previous,
            volume=volume,
            trade_status=trading,
            is_st=is_st,
            available_at=datetime(2024, 2, 1, 7, tzinfo=UTC),
        )
        for code, (price, previous, volume, trading, is_st) in specs.items()
    )
    return DecisionSnapshot(
        as_of=datetime(2024, 2, 1, 8, tzinfo=UTC),
        decision_date=decision_date,
        execution_deadline=datetime(2024, 2, 2, 1, 35, tzinfo=UTC),
        cash=Decimal("100000"),
        candidate_codes=("sh.600001", "sh.600002", "sh.600003", "sh.600006"),
        bars=bars,
        positions=(
            PortfolioPosition(
                code="sh.600004",
                quantity=100,
                sellable_quantity=0,
                average_cost=Decimal("10"),
            ),
            PortfolioPosition(
                code="sh.600005",
                quantity=100,
                sellable_quantity=100,
                average_cost=Decimal("10"),
            ),
        ),
        rules=tuple(
            InstrumentRule(code=code, lot_size=100, price_limit_ratio=Decimal("0.10"))
            for code in specs
        ),
        source_payloads=(PAYLOAD,),
    )


def test_risk_and_execution_never_emit_orders_against_a_share_constraints() -> None:
    snapshot = _constraint_snapshot()
    target = TargetPortfolio(
        snapshot_id=snapshot.snapshot_id,
        version="fixture-target-v1",
        weights=tuple(
            TargetWeight(code=code, weight=Decimal("0.10"), rank=rank)
            for rank, code in enumerate(snapshot.candidate_codes, start=1)
        ),
        cash_weight=Decimal("0.60"),
    )
    risk = HardRiskEngine(RiskConfig(allow_st_buys=True)).evaluate(snapshot, target)
    plan = ExecutionPlanner().plan(snapshot, risk)

    reasons = {rejection.code: rejection.reason for rejection in risk.rejections}
    assert reasons == {
        "sh.600001": "limit_up",
        "sh.600002": "suspended",
        "sh.600003": "limit_up",
        "sh.600004": "t_plus_one",
        "sh.600005": "limit_down",
    }
    assert {order.code for order in plan.orders} == {"sh.600006"}
    assert all(order.quantity > 0 and order.quantity % 100 == 0 for order in plan.orders)
    assert plan.estimated_cash_after_orders >= 0


def test_execution_planner_reserves_fees_before_buying() -> None:
    code = "sh.600000"
    day = date(2024, 2, 1)
    snapshot = DecisionSnapshot(
        as_of=datetime(2024, 2, 1, 8, tzinfo=UTC),
        decision_date=day,
        execution_deadline=datetime(2024, 2, 2, 1, 35, tzinfo=UTC),
        cash=Decimal("1010"),
        candidate_codes=(code,),
        bars=(
            DailyBar(
                code=code,
                trade_date=day,
                signal_close=Decimal("10"),
                execution_close=Decimal("10"),
                previous_close=Decimal("10"),
                volume=100_000,
                trade_status=True,
                is_st=False,
                available_at=datetime(2024, 2, 1, 7, tzinfo=UTC),
            ),
        ),
        positions=(),
        rules=(InstrumentRule(code=code, lot_size=100, price_limit_ratio=Decimal("0.10")),),
        source_payloads=(PAYLOAD,),
    )
    target = TargetPortfolio(
        snapshot_id=snapshot.snapshot_id,
        version="fixture-target-v1",
        weights=(TargetWeight(code=code, weight=Decimal(1), rank=1),),
        cash_weight=Decimal(0),
    )
    risk = HardRiskEngine(
        RiskConfig(max_position_weight=Decimal(1), max_gross_exposure=Decimal(1))
    ).evaluate(snapshot, target)

    plan = ExecutionPlanner().plan(snapshot, risk)

    assert len(plan.orders) == 1
    assert plan.orders[0].quantity == 100
    assert plan.orders[0].estimated_fees == Decimal("5.01")
    assert plan.estimated_cash_after_orders == Decimal("4.99")


def test_execution_planner_fails_closed_when_sell_fees_exceed_cash_and_proceeds() -> None:
    code = "sh.600000"
    day = date(2024, 2, 1)
    snapshot = DecisionSnapshot(
        as_of=datetime(2024, 2, 1, 8, tzinfo=UTC),
        decision_date=day,
        execution_deadline=datetime(2024, 2, 2, 1, 35, tzinfo=UTC),
        cash=Decimal(0),
        candidate_codes=(),
        bars=(
            DailyBar(
                code=code,
                trade_date=day,
                signal_close=Decimal(1),
                execution_close=Decimal(1),
                previous_close=Decimal(1),
                volume=100_000,
                trade_status=True,
                is_st=False,
                available_at=datetime(2024, 2, 1, 7, tzinfo=UTC),
            ),
        ),
        positions=(
            PortfolioPosition(
                code=code,
                quantity=1,
                sellable_quantity=1,
                average_cost=Decimal(1),
            ),
        ),
        rules=(InstrumentRule(code=code, lot_size=100, price_limit_ratio=Decimal("0.10")),),
        source_payloads=(PAYLOAD,),
    )
    target = TargetPortfolio(
        snapshot_id=snapshot.snapshot_id,
        version="fixture-target-v1",
        weights=(),
        cash_weight=Decimal(1),
    )
    risk = HardRiskEngine().evaluate(snapshot, target)

    with pytest.raises(RiskValidationError, match="negative cash"):
        ExecutionPlanner().plan(snapshot, risk)
