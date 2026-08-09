import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from trading_codex.api.dependencies import get_ledger
from trading_codex.ledger.jobs import RetryableDailyJobs
from trading_codex.ledger.models import (
    AlertPhase,
    CashMovementKind,
    ForwardObservation,
    JobStatus,
    JobType,
    LedgerConflictError,
    LedgerInvariantError,
    PortfolioTrack,
    ProviderHealthState,
)
from trading_codex.ledger.store import SQLiteLedger
from trading_codex.main import app
from trading_codex.operations.backup import BackupError, create_backup, replay_backup, verify_backup
from trading_codex.operations.health import (
    ProbeResult,
    ProviderHealthGateError,
    ProviderHealthMonitor,
    SQLiteIntegrityProbe,
)
from trading_codex.operations.review import (
    ForwardReviewBuilder,
    ObservationWindowError,
    write_forward_review,
)
from trading_codex.operations.scheduler import (
    DailySchedule,
    DailyScheduleError,
    OneShotDailyScheduler,
    ParquetTradingCalendar,
)


@dataclass(frozen=True)
class _Probe:
    name: str
    critical: bool
    result: ProbeResult | Exception

    def check(self, *, as_of: datetime) -> ProbeResult:
        assert as_of.tzinfo is not None
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass(frozen=True)
class _Calendar:
    trading_day: bool = True
    error: Exception | None = None

    def is_trading_day(self, day: date, *, as_of: datetime) -> bool:
        assert day == as_of.astimezone().date() or as_of.tzinfo is not None
        if self.error is not None:
            raise self.error
        return self.trading_day


@dataclass(frozen=True)
class _CalendarStore:
    rows: tuple[dict[str, object], ...]

    def rows_as_of(self, name: str, *, as_of: datetime) -> list[dict[str, object]]:
        assert name == "trade_calendar"
        assert as_of.tzinfo is not None
        return [dict(row) for row in self.rows]


def _health(
    state: ProviderHealthState, *, name: str = "market_data", critical: bool = True
) -> _Probe:
    return _Probe(
        name=name,
        critical=critical,
        result=ProbeResult(state=state, detail=f"{name} is {state.value}"),
    )


def _seed_decision_pair(ledger: SQLiteLedger, trading_date: date) -> tuple[str, str]:
    base_id = f"base-{trading_date.isoformat()}"
    shadow_id = f"shadow-{trading_date.isoformat()}"
    snapshot_id = f"snapshot-{trading_date.isoformat()}"
    as_of = datetime.combine(trading_date, time(1), tzinfo=UTC)
    recorded = as_of + timedelta(minutes=1)
    expires = as_of + timedelta(days=1)
    source_payloads = '["' + ("a" * 64) + '"]'
    with sqlite3.connect(ledger.path) as connection:
        for decision_id, track, configuration in (
            (base_id, PortfolioTrack.BASE, "base-config"),
            (shadow_id, PortfolioTrack.AI_SHADOW, "shadow-config"),
        ):
            connection.execute(
                """
                INSERT INTO decision_runs (
                    decision_id, snapshot_id, configuration_id, pipeline_version,
                    regime_version, allocator_version, portfolio_track, as_of,
                    decision_date, expires_at, source_payloads_json,
                    decision_payload_json, snapshot_payload_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    snapshot_id,
                    configuration,
                    "fixture-pipeline-v1",
                    "fixture-regime-v1",
                    "fixture-allocator-v1",
                    track.value,
                    as_of.isoformat().replace("+00:00", "Z"),
                    trading_date.isoformat(),
                    expires.isoformat().replace("+00:00", "Z"),
                    source_payloads,
                    "{}",
                    "{}",
                    recorded.isoformat().replace("+00:00", "Z"),
                ),
            )
    return base_id, shadow_id


def _observation(index: int) -> ForwardObservation:
    trading_date = date(2026, 1, 1) + timedelta(days=index)
    return ForwardObservation(
        observation_id=f"observation-{index}",
        trading_date=trading_date,
        observed_at=datetime.combine(trading_date, time(8), tzinfo=UTC),
        base_decision_id=f"base-{index}",
        ai_shadow_decision_id=f"shadow-{index}",
        snapshot_id=f"snapshot-{index}",
        base_configuration_id="base-config-v1",
        ai_shadow_configuration_id="shadow-config-v1",
        benchmark_return=Decimal(0),
        base_target_return=Decimal("0.01"),
        base_simulated_return=Decimal("0.009"),
        ai_shadow_return=Decimal("0.011"),
        actual_return=Decimal("0.008"),
        transaction_cost_rate=Decimal("0.001"),
        source_payloads=("a" * 64,),
        metric_payload_sha256="b" * 64,
    )


def test_v4_operational_tables_are_append_only(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    checked_at = datetime(2026, 8, 10, 1, tzinfo=UTC)
    check = ledger.record_provider_health(
        provider="market_data",
        state=ProviderHealthState.HEALTHY,
        critical=True,
        checked_at=checked_at,
        latency_ms=4,
        detail="cache and adapter are ready",
    )

    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"provider_health_checks", "alert_events", "forward_observations"} <= tables
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE provider_health_checks SET detail = 'changed' WHERE check_id = ?",
                (check.check_id,),
            )


def test_provider_health_gate_records_alert_transitions(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    monitor = ProviderHealthMonitor(ledger)
    failed_at = datetime(2026, 8, 10, 1, tzinfo=UTC)
    failed = monitor.check(
        (
            _health(ProviderHealthState.UNAVAILABLE),
            _health(
                ProviderHealthState.NOT_CONFIGURED,
                name="ai",
                critical=False,
            ),
        ),
        checked_at=failed_at,
    )
    assert not failed.ready
    with pytest.raises(ProviderHealthGateError, match="market_data=unavailable"):
        failed.require_ready()
    alerts = ledger.list_alerts(active_only=True)
    assert [(alert.alert_key, alert.phase) for alert in alerts] == [
        ("provider:market_data", AlertPhase.OPENED)
    ]

    recovered = monitor.check(
        (
            _health(ProviderHealthState.HEALTHY),
            _health(
                ProviderHealthState.NOT_CONFIGURED,
                name="ai",
                critical=False,
            ),
        ),
        checked_at=failed_at + timedelta(minutes=1),
    )
    assert recovered.ready
    assert ledger.list_alerts(active_only=True) == ()
    assert ledger.list_alerts()[0].phase is AlertPhase.RESOLVED

    optional_failure = monitor.check(
        (
            _health(ProviderHealthState.HEALTHY),
            _health(
                ProviderHealthState.UNAVAILABLE,
                name="ai",
                critical=False,
            ),
        ),
        checked_at=failed_at + timedelta(minutes=2),
    )
    assert optional_failure.ready
    assert ledger.list_alerts(active_only=True)[0].alert_key == "provider:ai"
    monitor.check(
        (
            _health(ProviderHealthState.HEALTHY),
            _health(
                ProviderHealthState.NOT_CONFIGURED,
                name="ai",
                critical=False,
            ),
        ),
        checked_at=failed_at + timedelta(minutes=3),
    )
    assert ledger.list_alerts(active_only=True) == ()


def test_scheduler_fails_closed_then_retries_once_after_recovery(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    scheduler = OneShotDailyScheduler(
        ledger,
        _Calendar(),
        schedule=DailySchedule(max_lateness=timedelta(minutes=10)),
    )
    now = datetime(2026, 8, 10, 1, 36, tzinfo=UTC)
    calls = 0

    def task() -> dict[str, object]:
        nonlocal calls
        calls += 1
        ledger.record_cash_movement(
            portfolio_track=PortfolioTrack.ACTUAL,
            kind=CashMovementKind.DEPOSIT,
            amount=Decimal("100"),
            occurred_at=now,
            idempotency_key="scheduler-cash",
        )
        return {"decision_id": "fixture"}

    tasks = {JobType.OPENING_DECISION: task}
    with pytest.raises(ProviderHealthGateError):
        scheduler.run_due(
            now=now,
            tasks=tasks,
            probes=(_health(ProviderHealthState.UNAVAILABLE),),
        )
    assert calls == 0
    assert ledger.list_cash_movements() == ()
    assert ledger.list_job_runs()[0].status is JobStatus.FAILED

    succeeded = scheduler.run_due(
        now=now + timedelta(minutes=1),
        tasks=tasks,
        probes=(_health(ProviderHealthState.HEALTHY),),
    )[0]
    assert succeeded.run.status is JobStatus.SUCCEEDED
    assert succeeded.run.attempts == 2
    assert succeeded.run.latest_result is not None
    assert len(succeeded.run.latest_result["health_check_ids"]) == 1
    assert calls == 1
    assert ledger.list_cash_movements()[0].amount == Decimal("100")
    assert ledger.list_alerts(active_only=True) == ()

    repeated = scheduler.run_due(
        now=now + timedelta(minutes=2),
        tasks=tasks,
        probes=(_health(ProviderHealthState.HEALTHY),),
    )[0]
    assert repeated.run == succeeded.run
    assert calls == 1


def test_scheduler_rejects_an_empty_critical_health_gate(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    scheduler = OneShotDailyScheduler(ledger, _Calendar())
    called = False

    def task() -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    with pytest.raises(ProviderHealthGateError, match="no critical"):
        scheduler.run_due(
            now=datetime(2026, 8, 10, 1, 36, tzinfo=UTC),
            tasks={JobType.OPENING_DECISION: task},
            probes=(),
        )
    assert called is False
    assert ledger.list_job_runs()[0].status is JobStatus.FAILED


def test_parquet_calendar_uses_explicit_as_of_and_rejects_missing_dates() -> None:
    day = date(2026, 8, 10)
    as_of = datetime(2026, 8, 10, 1, 36, tzinfo=UTC)
    calendar = ParquetTradingCalendar(
        _CalendarStore(rows=({"calendar_date": day, "is_trading_day": True},))
    )
    assert calendar.is_trading_day(day, as_of=as_of) is True
    with pytest.raises(DailyScheduleError, match="exactly one row"):
        ParquetTradingCalendar(_CalendarStore(rows=())).is_trading_day(day, as_of=as_of)


def test_job_attempt_lease_rejects_overlap_and_recovers_stale_run(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    scheduled_for = datetime(2026, 8, 10, 1, 35, tzinfo=UTC)
    run_id = ledger.ensure_job_run(JobType.OPENING_DECISION, scheduled_for)
    started_at = scheduled_for + timedelta(seconds=1)
    assert ledger.begin_job_attempt(
        run_id=run_id,
        attempt_id="first",
        occurred_at=started_at,
        stale_after=timedelta(minutes=5),
    )
    with pytest.raises(LedgerInvariantError, match="outcome cannot precede"):
        ledger.record_job_attempt_event(
            run_id=run_id,
            attempt_id="first",
            phase="failed",
            occurred_at=started_at - timedelta(seconds=1),
            error="fixture",
        )
    with pytest.raises(LedgerConflictError, match="active attempt"):
        ledger.begin_job_attempt(
            run_id=run_id,
            attempt_id="overlap",
            occurred_at=started_at + timedelta(minutes=1),
            stale_after=timedelta(minutes=5),
        )
    assert ledger.begin_job_attempt(
        run_id=run_id,
        attempt_id="recovery",
        occurred_at=started_at + timedelta(minutes=6),
        stale_after=timedelta(minutes=5),
    )
    running = ledger.job_run(run_id)
    assert running.status is JobStatus.RUNNING
    assert running.attempts == 2


def test_retryable_job_observes_an_existing_active_attempt_without_running_task(
    tmp_path: Path,
) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    scheduled_for = datetime.now(UTC)
    run_id = ledger.ensure_job_run(JobType.OPENING_DECISION, scheduled_for)
    ledger.begin_job_attempt(
        run_id=run_id,
        attempt_id="active",
        occurred_at=scheduled_for,
        stale_after=timedelta(minutes=5),
    )
    called = False

    def task() -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    observed = RetryableDailyJobs(ledger).run_opening_decision(
        scheduled_for=scheduled_for, task=task
    )
    assert observed.status is JobStatus.RUNNING
    assert called is False


def test_scheduler_skips_closed_days_and_alerts_calendar_failure(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 7, 31, tzinfo=UTC)
    ledger = SQLiteLedger(tmp_path / "closed.db")
    closed = OneShotDailyScheduler(ledger, _Calendar(trading_day=False))
    assert closed.run_due(now=now, tasks={}, probes=()) == ()
    assert ledger.list_job_runs() == ()

    failed_ledger = SQLiteLedger(tmp_path / "failed.db")
    failed = OneShotDailyScheduler(
        failed_ledger, _Calendar(error=RuntimeError("calendar missing"))
    )
    with pytest.raises(RuntimeError, match="trading calendar check failed"):
        failed.run_due(now=now, tasks={}, probes=())
    assert failed_ledger.list_alerts(active_only=True)[0].alert_key == (
        "scheduler:trading-calendar"
    )


def test_consistent_backup_verification_and_replay_are_point_in_time(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    first = datetime(2026, 8, 10, 1, tzinfo=UTC)
    ledger.record_cash_movement(
        portfolio_track=PortfolioTrack.ACTUAL,
        kind=CashMovementKind.DEPOSIT,
        amount=Decimal("100"),
        occurred_at=first,
        idempotency_key="first",
    )
    manifest = create_backup(
        ledger.path,
        tmp_path / "backups",
        created_at=first + timedelta(minutes=1),
    )
    verification = verify_backup(manifest.manifest_path, verified_at=first + timedelta(minutes=2))
    assert verification.fingerprint.integrity == "ok"

    ledger.record_cash_movement(
        portfolio_track=PortfolioTrack.ACTUAL,
        kind=CashMovementKind.DEPOSIT,
        amount=Decimal("50"),
        occurred_at=first + timedelta(minutes=3),
        idempotency_key="second",
    )
    replay = replay_backup(manifest.manifest_path, verified_at=first + timedelta(minutes=4))
    assert replay.track_cash[PortfolioTrack.ACTUAL.value] == "100"
    assert ledger.dashboard(as_of=first + timedelta(minutes=4)).tracks[2].cash == Decimal("150")
    assert replay.replay_schema_version == 4

    database_path = manifest.manifest_path.parent / manifest.database_file
    with database_path.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(BackupError, match="size|SHA-256"):
        verify_backup(manifest.manifest_path)


def test_backup_rejects_a_non_ledger_sqlite_database(tmp_path: Path) -> None:
    path = tmp_path / "not-ledger.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    with pytest.raises(BackupError, match="required ledger tables"):
        create_backup(path, tmp_path / "backups")


def test_sqlite_integrity_probe_is_read_only_and_reports_schema(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    result = SQLiteIntegrityProbe(ledger.path).check(
        as_of=datetime(2026, 8, 10, 1, tzinfo=UTC)
    )
    assert result.state is ProviderHealthState.HEALTHY
    assert result.metadata["schema_version"] == 4


def test_forward_observation_requires_matching_trace_and_is_idempotent(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    trading_date = date(2026, 8, 10)
    base_id, shadow_id = _seed_decision_pair(ledger, trading_date)
    arguments = {
        "trading_date": trading_date,
        "observed_at": datetime(2026, 8, 10, 8, tzinfo=UTC),
        "base_decision_id": base_id,
        "ai_shadow_decision_id": shadow_id,
        "benchmark_return": Decimal("0.001"),
        "base_target_return": Decimal("0.002"),
        "base_simulated_return": Decimal("0.0018"),
        "ai_shadow_return": Decimal("0.0021"),
        "actual_return": Decimal("0.0015"),
        "transaction_cost_rate": Decimal("0.0002"),
        "metric_payload_sha256": "b" * 64,
    }
    observation = ledger.record_forward_observation(**arguments)
    assert ledger.record_forward_observation(**arguments) == observation
    assert observation.snapshot_id == "snapshot-2026-08-10"
    assert observation.source_payloads == ("a" * 64,)
    with pytest.raises(LedgerConflictError):
        ledger.record_forward_observation(
            **{**arguments, "actual_return": Decimal("0.0014")}
        )


def test_forward_review_hard_gates_60_days_and_preserves_traces(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 60"):
        ForwardReviewBuilder(minimum_trading_days=59)
    builder = ForwardReviewBuilder(minimum_trading_days=60)
    observations = tuple(_observation(index) for index in range(60))
    with pytest.raises(ObservationWindowError, match="found 59"):
        builder.build(observations[:59])

    report = builder.build(observations)
    assert report.trading_days == 60
    assert report.attribution.strategy_vs_benchmark == report.cumulative_returns.base_target
    assert report.attribution.ai_overlay_vs_base_simulation == (
        report.cumulative_returns.ai_shadow - report.cumulative_returns.base_simulated
    )
    assert report.average_transaction_cost_rate == Decimal("0.001")
    assert report.traces[0].base_decision_id == "base-0"
    assert builder.build(tuple(reversed(observations))).report_id == report.report_id
    path = write_forward_review(report, tmp_path / "reports")
    assert write_forward_review(report, tmp_path / "reports") == path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_operations_api_is_read_only_and_reports_activation(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "api.db")
    checked_at = datetime(2026, 8, 10, 1, tzinfo=UTC)
    ProviderHealthMonitor(ledger).check(
        (_health(ProviderHealthState.HEALTHY),), checked_at=checked_at
    )

    async def override_ledger() -> SQLiteLedger:
        return ledger

    app.dependency_overrides[get_ledger] = override_ledger
    transport = httpx2.ASGITransport(app=app)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/operations/status",
                params={"as_of": (checked_at + timedelta(minutes=1)).isoformat()},
            )
            observations = await client.get("/api/v1/operations/observations")
            stale_response = await client.get(
                "/api/v1/operations/status",
                params={"as_of": (checked_at + timedelta(minutes=16)).isoformat()},
            )
    finally:
        app.dependency_overrides.pop(get_ledger, None)
    assert response.status_code == 200
    payload = response.json()
    assert payload["scheduler_mode"] == "external_one_shot"
    assert payload["scheduler_activated"] is False
    assert payload["health_gate_ready"] is True
    assert payload["provider_health"][0]["state"] == "healthy"
    assert payload["observed_trading_days"] == 0
    assert payload["review_ready"] is False
    assert observations.json() == []
    assert stale_response.json()["health_gate_ready"] is False
