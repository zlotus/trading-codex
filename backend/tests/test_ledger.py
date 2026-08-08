import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from pathlib import Path

import httpx2
import pytest

from trading_codex.api.dependencies import get_ledger
from trading_codex.domain.contracts import (
    DecisionRun,
    ExecutionPlan,
    FeatureSet,
    OrderSide,
    PlannedOrder,
    RiskDecision,
    StrategyProposal,
    TargetPortfolio,
)
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import DailyBar, DecisionSnapshot, InstrumentRule
from trading_codex.ledger.jobs import RetryableDailyJobs
from trading_codex.ledger.models import (
    CashMovementKind,
    JobStatus,
    LedgerConflictError,
    LedgerInvariantError,
    PortfolioTrack,
    SignalStatus,
)
from trading_codex.ledger.store import SQLiteLedger
from trading_codex.main import app

CODE = "sh.600000"


def _decision(
    *,
    as_of: datetime,
    side: OrderSide,
    quantity: int,
    price: Decimal = Decimal("10"),
    deadline: datetime | None = None,
    key: str,
) -> tuple[DecisionSnapshot, DecisionRun]:
    expires_at = deadline or as_of + timedelta(days=2)
    decision_date = as_of.astimezone().date()
    snapshot = DecisionSnapshot(
        as_of=as_of,
        decision_date=decision_date,
        execution_deadline=expires_at,
        cash=Decimal("20000"),
        candidate_codes=(CODE,) if side is OrderSide.BUY else (),
        bars=(
            DailyBar(
                code=CODE,
                trade_date=decision_date,
                signal_close=price,
                execution_close=price,
                previous_close=price,
                volume=100_000,
                trade_status=True,
                is_st=False,
                available_at=as_of - timedelta(minutes=1),
            ),
        ),
        positions=(),
        rules=(InstrumentRule(code=CODE, lot_size=100, price_limit_ratio=Decimal("0.10")),),
        source_payloads=(canonical_sha256({"fixture": key}),),
    )
    target = TargetPortfolio(
        snapshot_id=snapshot.snapshot_id,
        version="ledger-fixture-target-v1",
        weights=(),
        cash_weight=Decimal(1),
    )
    execution = ExecutionPlan(
        snapshot_id=snapshot.snapshot_id,
        version="ledger-fixture-execution-v1",
        orders=(
            PlannedOrder(
                code=CODE,
                side=side,
                quantity=quantity,
                reference_price=price,
                estimated_fees=Decimal("5"),
                target_weight=Decimal("0.10") if side is OrderSide.BUY else Decimal(0),
                expires_at=expires_at,
            ),
        ),
        estimated_cash_after_orders=Decimal("17995"),
    )
    run = DecisionRun(
        decision_id="pending",
        snapshot_id=snapshot.snapshot_id,
        configuration_id=canonical_sha256({"configuration": "ledger-fixture-v1"}),
        pipeline_version="ledger-fixture-pipeline-v1",
        features=FeatureSet(
            snapshot_id=snapshot.snapshot_id,
            as_of=as_of,
            version="ledger-fixture-features-v1",
            features=(),
            candidates=(),
            exclusions=(),
        ),
        proposal=StrategyProposal(
            snapshot_id=snapshot.snapshot_id,
            version="ledger-fixture-strategy-v1",
            intents=(),
        ),
        allocated=target,
        risk=RiskDecision(
            snapshot_id=snapshot.snapshot_id,
            version="ledger-fixture-risk-v1",
            equity=Decimal("20000"),
            requested=target,
            approved_weights=(),
            rejections=(),
        ),
        execution=execution,
    )
    run = replace(
        run,
        decision_id=canonical_sha256(
            {
                "snapshot_id": snapshot.snapshot_id,
                "configuration_id": run.configuration_id,
                "features": run.features,
                "proposal": run.proposal,
                "allocated": run.allocated,
                "risk": run.risk,
                "execution": run.execution,
            }
        ),
    )
    return snapshot, run


def _seed_cash(
    ledger: SQLiteLedger,
    track: PortfolioTrack,
    occurred_at: datetime,
    *,
    key: str,
) -> None:
    ledger.record_cash_movement(
        portfolio_track=track,
        kind=CashMovementKind.DEPOSIT,
        amount=Decimal("20000"),
        occurred_at=occurred_at,
        idempotency_key=key,
    )


def test_decision_trace_rejects_forged_id_and_mixed_snapshot_stage(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    as_of = datetime(2024, 1, 2, 1, tzinfo=UTC)
    snapshot, run = _decision(
        as_of=as_of,
        side=OrderSide.BUY,
        quantity=100,
        key="decision-integrity",
    )
    with pytest.raises(LedgerInvariantError, match="decision id"):
        ledger.record_decision(snapshot, replace(run, decision_id="0" * 64))

    mixed = replace(
        run,
        features=replace(run.features, snapshot_id="f" * 64),
    )
    with pytest.raises(LedgerInvariantError, match="decision stage"):
        ledger.record_decision(snapshot, mixed)


def test_partial_fills_fees_t1_and_three_track_reconciliation(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    buy_as_of = datetime(2024, 2, 1, 1, tzinfo=UTC)
    snapshot, run = _decision(
        as_of=buy_as_of,
        side=OrderSide.BUY,
        quantity=200,
        key="buy",
    )
    signal_id = ledger.record_decision(snapshot, run)[0]
    assert ledger.record_decision(snapshot, run) == (signal_id,)
    detail = ledger.signal_detail(signal_id, as_of=buy_as_of)
    order_id = detail.signal.order_intent_id

    for track in (PortfolioTrack.BASE, PortfolioTrack.ACTUAL):
        _seed_cash(
            ledger,
            track,
            buy_as_of - timedelta(hours=1),
            key=f"opening-{track.value}",
        )
    ledger.record_fill(
        source_order_intent_id=order_id,
        portfolio_track=PortfolioTrack.BASE,
        quantity=200,
        price=Decimal("10"),
        fees=Decimal("5"),
        occurred_at=buy_as_of + timedelta(minutes=5),
        idempotency_key="base-full-fill",
    )
    actual_fill = ledger.record_fill(
        source_order_intent_id=order_id,
        portfolio_track=PortfolioTrack.ACTUAL,
        quantity=100,
        price=Decimal("10"),
        fees=Decimal("5"),
        occurred_at=buy_as_of + timedelta(minutes=6),
        idempotency_key="actual-partial-fill",
    )
    before_fill = ledger.dashboard(as_of=buy_as_of)
    before_actual = {
        track.track: track for track in before_fill.tracks
    }[PortfolioTrack.ACTUAL]
    assert before_actual.positions == ()
    assert before_fill.signals[0].status is SignalStatus.ACTIVE
    assert (
        ledger.record_fill(
            source_order_intent_id=order_id,
            portfolio_track=PortfolioTrack.ACTUAL,
            quantity=100,
            price=Decimal("10"),
            fees=Decimal("5"),
            occurred_at=buy_as_of + timedelta(minutes=6),
            idempotency_key="actual-partial-fill",
        )
        == actual_fill
    )

    dashboard = ledger.dashboard(as_of=buy_as_of + timedelta(hours=1))
    tracks = {track.track: track for track in dashboard.tracks}
    actual = tracks[PortfolioTrack.ACTUAL]
    assert actual.cash == Decimal("18995")
    assert actual.positions[0].quantity == 100
    assert actual.positions[0].sellable_quantity == 0
    assert dashboard.signals[0].status is SignalStatus.PARTIAL
    assert dashboard.reconciliation.rows[0].actual_vs_base == -100
    assert [movement.kind for movement in ledger.list_cash_movements(
        portfolio_track=PortfolioTrack.ACTUAL
    )] == [CashMovementKind.DEPOSIT, CashMovementKind.TRADE, CashMovementKind.FEE]

    sell_as_of = buy_as_of + timedelta(hours=2)
    sell_snapshot, sell_run = _decision(
        as_of=sell_as_of,
        side=OrderSide.SELL,
        quantity=100,
        price=Decimal("11"),
        deadline=buy_as_of + timedelta(days=3),
        key="sell",
    )
    sell_signal = ledger.record_decision(sell_snapshot, sell_run)[0]
    sell_order = ledger.signal_detail(sell_signal, as_of=sell_as_of).signal.order_intent_id

    with pytest.raises(LedgerInvariantError, match=r"T\+1"):
        ledger.record_fill(
            source_order_intent_id=sell_order,
            portfolio_track=PortfolioTrack.ACTUAL,
            quantity=100,
            price=Decimal("11"),
            fees=Decimal("6"),
            occurred_at=buy_as_of + timedelta(hours=3),
            idempotency_key="same-day-sell",
        )
    assert len(ledger.list_fills(portfolio_track=PortfolioTrack.ACTUAL)) == 1

    ledger.record_fill(
        source_order_intent_id=sell_order,
        portfolio_track=PortfolioTrack.ACTUAL,
        quantity=100,
        price=Decimal("11"),
        fees=Decimal("6"),
        occurred_at=buy_as_of + timedelta(days=1),
        idempotency_key="next-day-sell",
    )
    next_day = ledger.dashboard(as_of=buy_as_of + timedelta(days=1, hours=1))
    next_actual = {track.track: track for track in next_day.tracks}[PortfolioTrack.ACTUAL]
    assert next_actual.cash == Decimal("20089")
    assert next_actual.positions == ()

    trace = ledger.signal_detail(signal_id, as_of=buy_as_of).trace
    assert trace.snapshot_id == snapshot.snapshot_id
    assert trace.source_payloads == snapshot.source_payloads

    with sqlite3.connect(tmp_path / "ledger.db") as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE fills SET quantity = 1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM fills")


def test_partial_fill_can_skip_remainder_but_cannot_fill_after_skip(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    as_of = datetime(2024, 3, 1, 1, tzinfo=UTC)
    snapshot, run = _decision(
        as_of=as_of,
        side=OrderSide.BUY,
        quantity=200,
        key="skip",
    )
    signal_id = ledger.record_decision(snapshot, run)[0]
    order_id = ledger.signal_detail(signal_id, as_of=as_of).signal.order_intent_id
    _seed_cash(ledger, PortfolioTrack.ACTUAL, as_of - timedelta(hours=1), key="opening")
    ledger.record_fill(
        source_order_intent_id=order_id,
        portfolio_track=PortfolioTrack.ACTUAL,
        quantity=100,
        price=Decimal("10"),
        fees=Decimal("5"),
        occurred_at=as_of + timedelta(minutes=1),
        idempotency_key="partial",
    )
    skipped = ledger.skip_signal(
        signal_id,
        portfolio_track=PortfolioTrack.ACTUAL,
        reason="人工放弃剩余数量",
        occurred_at=as_of + timedelta(minutes=2),
        idempotency_key="skip-remainder",
    )
    assert skipped.status is SignalStatus.SKIPPED
    assert skipped.filled_quantity == 100
    assert skipped.remaining_quantity == 100
    before_skip = ledger.signal_detail(
        signal_id, as_of=as_of + timedelta(minutes=1)
    )
    assert before_skip.signal.status is SignalStatus.PARTIAL

    with pytest.raises(LedgerInvariantError, match="skipped"):
        ledger.record_fill(
            source_order_intent_id=order_id,
            portfolio_track=PortfolioTrack.ACTUAL,
            quantity=100,
            price=Decimal("10"),
            fees=Decimal("5"),
            occurred_at=as_of + timedelta(minutes=3),
            idempotency_key="after-skip",
        )


def test_ledger_rejects_negative_cash_and_idempotency_conflicts(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    as_of = datetime(2024, 4, 1, 1, tzinfo=UTC)
    snapshot, run = _decision(
        as_of=as_of,
        side=OrderSide.BUY,
        quantity=100,
        key="negative-cash",
    )
    signal_id = ledger.record_decision(snapshot, run)[0]
    order_id = ledger.signal_detail(signal_id, as_of=as_of).signal.order_intent_id
    with pytest.raises(LedgerInvariantError, match="negative"):
        ledger.record_fill(
            source_order_intent_id=order_id,
            portfolio_track=PortfolioTrack.ACTUAL,
            quantity=100,
            price=Decimal("10"),
            fees=Decimal("5"),
            occurred_at=as_of + timedelta(minutes=1),
            idempotency_key="unfunded-fill",
        )
    assert ledger.list_fills() == ()

    _seed_cash(ledger, PortfolioTrack.ACTUAL, as_of - timedelta(hours=1), key="opening")
    ledger.record_fill(
        source_order_intent_id=order_id,
        portfolio_track=PortfolioTrack.ACTUAL,
        quantity=100,
        price=Decimal("10"),
        fees=Decimal("5"),
        occurred_at=as_of + timedelta(minutes=1),
        idempotency_key="fill-key",
    )
    with pytest.raises(LedgerConflictError, match="different content"):
        ledger.record_fill(
            source_order_intent_id=order_id,
            portfolio_track=PortfolioTrack.ACTUAL,
            quantity=99,
            price=Decimal("10"),
            fees=Decimal("5"),
            occurred_at=as_of + timedelta(minutes=1),
            idempotency_key="fill-key",
        )


def test_as_of_text_order_is_exact_within_the_same_second(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    second = datetime(2024, 4, 2, 1, tzinfo=UTC)
    ledger.record_cash_movement(
        portfolio_track=PortfolioTrack.ACTUAL,
        kind=CashMovementKind.DEPOSIT,
        amount=Decimal("100"),
        occurred_at=second + timedelta(microseconds=500_000),
        idempotency_key="subsecond-deposit",
    )

    before = ledger.dashboard(as_of=second)
    after = ledger.dashboard(as_of=second + timedelta(microseconds=500_000))
    before_actual = {track.track: track for track in before.tracks}[PortfolioTrack.ACTUAL]
    after_actual = {track.track: track for track in after.tracks}[PortfolioTrack.ACTUAL]
    assert before_actual.cash == 0
    assert after_actual.cash == 100


def test_ledger_projection_does_not_inherit_process_decimal_context(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    as_of = datetime(2024, 4, 3, 1, tzinfo=UTC)
    snapshot, run = _decision(
        as_of=as_of,
        side=OrderSide.BUY,
        quantity=100,
        price=Decimal("10.123456789"),
        key="decimal-context",
    )
    signal_id = ledger.record_decision(snapshot, run)[0]
    order_id = ledger.signal_detail(signal_id, as_of=as_of).signal.order_intent_id
    _seed_cash(ledger, PortfolioTrack.ACTUAL, as_of - timedelta(hours=1), key="cash")
    ledger.record_fill(
        source_order_intent_id=order_id,
        portfolio_track=PortfolioTrack.ACTUAL,
        quantity=100,
        price=Decimal("10.123456789"),
        fees=Decimal("5.01"),
        occurred_at=as_of + timedelta(minutes=1),
        idempotency_key="fill",
    )

    with localcontext(Context(prec=10)):
        low_precision = ledger.dashboard(as_of=as_of + timedelta(minutes=2))
    with localcontext(Context(prec=50)):
        high_precision = ledger.dashboard(as_of=as_of + timedelta(minutes=2))
    assert low_precision == high_precision


def test_daily_job_run_keeps_failed_attempt_and_retries_idempotently(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    jobs = RetryableDailyJobs(ledger)
    scheduled_for = datetime(2024, 5, 1, 7, 35, tzinfo=UTC)
    calls = 0

    def task() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("provider unavailable")
        return {"decision_id": "decision-1"}

    with pytest.raises(RuntimeError, match="provider unavailable"):
        jobs.run_opening_decision(scheduled_for=scheduled_for, task=task)
    failed = ledger.list_job_runs()[0]
    assert failed.status is JobStatus.FAILED
    assert failed.attempts == 1
    assert failed.latest_error == "RuntimeError: provider unavailable"

    succeeded = jobs.run_opening_decision(scheduled_for=scheduled_for, task=task)
    assert succeeded.status is JobStatus.SUCCEEDED
    assert succeeded.attempts == 2
    assert succeeded.latest_result == {"decision_id": "decision-1"}
    assert jobs.run_opening_decision(scheduled_for=scheduled_for, task=task) == succeeded
    assert calls == 2


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def ledger_client(tmp_path: Path) -> httpx2.AsyncClient:
    ledger = SQLiteLedger(tmp_path / "api-ledger.db")
    as_of = datetime(2026, 8, 8, 1, tzinfo=UTC)
    snapshot, run = _decision(
        as_of=as_of,
        side=OrderSide.BUY,
        quantity=200,
        deadline=datetime(2026, 8, 9, 8, tzinfo=UTC),
        key="api",
    )
    ledger.record_decision(snapshot, run)
    _seed_cash(ledger, PortfolioTrack.ACTUAL, as_of - timedelta(hours=1), key="api-opening")
    async def override_ledger() -> SQLiteLedger:
        return ledger

    app.dependency_overrides[get_ledger] = override_ledger
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.pop(get_ledger, None)


@pytest.mark.anyio
async def test_ledger_api_records_partial_fill_and_returns_trace(
    ledger_client: httpx2.AsyncClient,
) -> None:
    dashboard = await ledger_client.get("/api/v1/ledger/dashboard")
    assert dashboard.status_code == 200
    signal = dashboard.json()["signals"][0]
    response = await ledger_client.post(
        "/api/v1/ledger/fills",
        json={
            "source_order_intent_id": signal["order_intent_id"],
            "portfolio_track": "actual",
            "quantity": 100,
            "price": "10.05",
            "fees": "5.01",
            "occurred_at": "2026-08-08T02:00:00Z",
            "idempotency_key": "api-partial-fill",
            "note": "券商回填",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["signal"]["status"] == "partial"
    assert payload["signal"]["filled_quantity"] == 100
    assert payload["trace"]["snapshot_id"] == signal["snapshot_id"]
    assert payload["price_points"]

    replay = await ledger_client.post(
        "/api/v1/ledger/fills",
        json={
            "source_order_intent_id": signal["order_intent_id"],
            "portfolio_track": "actual",
            "quantity": 100,
            "price": "10.05",
            "fees": "5.01",
            "occurred_at": "2026-08-08T02:00:00Z",
            "idempotency_key": "api-partial-fill",
            "note": "券商回填",
        },
    )
    assert replay.status_code == 201


@pytest.mark.anyio
async def test_ledger_api_maps_missing_and_conflicting_events(
    ledger_client: httpx2.AsyncClient,
) -> None:
    missing = await ledger_client.get("/api/v1/ledger/signals/not-found")
    assert missing.status_code == 404

    dashboard = (await ledger_client.get("/api/v1/ledger/dashboard")).json()
    order_id = dashboard["signals"][0]["order_intent_id"]
    request = {
        "source_order_intent_id": order_id,
        "portfolio_track": "actual",
        "quantity": 100,
        "price": "10",
        "fees": "5",
        "occurred_at": "2026-08-08T02:00:00Z",
        "idempotency_key": "conflict-key",
    }
    assert (await ledger_client.post("/api/v1/ledger/fills", json=request)).status_code == 201
    request["quantity"] = 99
    conflict = await ledger_client.post("/api/v1/ledger/fills", json=request)
    assert conflict.status_code == 409


@pytest.mark.anyio
async def test_ledger_api_allows_only_actual_manual_cash_events(
    ledger_client: httpx2.AsyncClient,
) -> None:
    request = {
        "portfolio_track": "base",
        "kind": "deposit",
        "amount": "50",
        "occurred_at": "2026-08-08T02:00:00Z",
        "idempotency_key": "api-extra-cash",
    }
    rejected = await ledger_client.post("/api/v1/ledger/cash-movements", json=request)
    assert rejected.status_code == 422

    request["portfolio_track"] = "actual"
    accepted = await ledger_client.post("/api/v1/ledger/cash-movements", json=request)
    assert accepted.status_code == 201
    actual = next(
        track for track in accepted.json()["tracks"] if track["track"] == "actual"
    )
    assert actual["cash"] == "20050"


@pytest.mark.anyio
async def test_ledger_api_skips_remaining_signal_append_only(
    ledger_client: httpx2.AsyncClient,
) -> None:
    dashboard = (await ledger_client.get("/api/v1/ledger/dashboard")).json()
    signal = dashboard["signals"][0]
    skipped = await ledger_client.post(
        f"/api/v1/ledger/signals/{signal['signal_id']}/skip",
        json={
            "portfolio_track": "actual",
            "reason": "人工放弃本次信号",
            "occurred_at": "2026-08-08T02:00:00Z",
            "idempotency_key": "api-skip-signal",
        },
    )
    assert skipped.status_code == 200
    assert skipped.json()["signal"]["status"] == "skipped"

    fill_after_skip = await ledger_client.post(
        "/api/v1/ledger/fills",
        json={
            "source_order_intent_id": signal["order_intent_id"],
            "portfolio_track": "actual",
            "quantity": 100,
            "price": "10",
            "fees": "5",
            "occurred_at": "2026-08-08T02:01:00Z",
            "idempotency_key": "api-fill-after-skip",
        },
    )
    assert fill_after_skip.status_code == 422
