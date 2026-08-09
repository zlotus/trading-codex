from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.ledger.jobs import JobTask, RetryableDailyJobs
from trading_codex.ledger.models import AlertSeverity, JobRunView, JobStatus, JobType, as_utc
from trading_codex.ledger.store import SQLiteLedger
from trading_codex.operations.health import ProviderHealthMonitor, ProviderProbe

SHANGHAI = ZoneInfo("Asia/Shanghai")


class DailyScheduleError(RuntimeError):
    """A due daily run cannot be planned or completed safely."""


class TradingCalendar(Protocol):
    def is_trading_day(self, day: date, *, as_of: datetime) -> bool: ...


@dataclass(frozen=True)
class ParquetTradingCalendar:
    store: ParquetDataStore

    def is_trading_day(self, day: date, *, as_of: datetime) -> bool:
        boundary = as_utc(as_of, field="as_of")
        rows = [
            row
            for row in self.store.rows_as_of("trade_calendar", as_of=boundary)
            if row["calendar_date"] == day
        ]
        if len(rows) != 1:
            raise DailyScheduleError(
                f"trade calendar must contain exactly one row for {day.isoformat()}"
            )
        return bool(rows[0]["is_trading_day"])


@dataclass(frozen=True)
class DailySchedule:
    opening_time: time = time(9, 35)
    eod_time: time = time(15, 30)
    max_lateness: timedelta = timedelta(minutes=20)

    def __post_init__(self) -> None:
        if self.opening_time.tzinfo is not None or self.eod_time.tzinfo is not None:
            raise ValueError("daily schedule times must be timezone-naive Shanghai wall times")
        if not timedelta(0) <= self.max_lateness <= timedelta(hours=2):
            raise ValueError("daily schedule max_lateness must be between zero and two hours")


@dataclass(frozen=True)
class ScheduledRun:
    job_type: JobType
    scheduled_for: datetime


@dataclass(frozen=True)
class DailyRunResult:
    planned: ScheduledRun
    run: JobRunView


class OneShotDailyScheduler:
    """Runs due jobs once; an external timer owns wake-up and process supervision."""

    def __init__(
        self,
        ledger: SQLiteLedger,
        calendar: TradingCalendar,
        *,
        schedule: DailySchedule | None = None,
    ) -> None:
        self.ledger = ledger
        self.calendar = calendar
        self.schedule = schedule or DailySchedule()
        self.jobs = RetryableDailyJobs(ledger)
        self.health = ProviderHealthMonitor(ledger)

    def due_jobs(self, *, now: datetime) -> tuple[ScheduledRun, ...]:
        boundary = as_utc(now, field="now")
        local = boundary.astimezone(SHANGHAI)
        candidates = (
            (JobType.OPENING_DECISION, self.schedule.opening_time),
            (JobType.EOD_PREPARATION, self.schedule.eod_time),
        )
        due: list[ScheduledRun] = []
        for job_type, wall_time in candidates:
            scheduled = datetime.combine(local.date(), wall_time, tzinfo=SHANGHAI)
            scheduled_utc = scheduled.astimezone(boundary.tzinfo)
            if scheduled_utc <= boundary <= scheduled_utc + self.schedule.max_lateness:
                due.append(ScheduledRun(job_type=job_type, scheduled_for=scheduled_utc))
        return tuple(due)

    def run_due(
        self,
        *,
        now: datetime,
        tasks: Mapping[JobType, JobTask],
        probes: tuple[ProviderProbe, ...],
    ) -> tuple[DailyRunResult, ...]:
        boundary = as_utc(now, field="now")
        planned = self.due_jobs(now=boundary)
        if not planned:
            return ()
        local_day = boundary.astimezone(SHANGHAI).date()
        try:
            is_trading_day = self.calendar.is_trading_day(local_day, as_of=boundary)
        except Exception as error:
            self.ledger.transition_alert(
                alert_key="scheduler:trading-calendar",
                active=True,
                severity=AlertSeverity.CRITICAL,
                message=f"{type(error).__name__}: {error}",
                occurred_at=boundary,
                context={"trading_date": local_day.isoformat()},
            )
            raise DailyScheduleError("trading calendar check failed") from error
        self.ledger.transition_alert(
            alert_key="scheduler:trading-calendar",
            active=False,
            severity=AlertSeverity.WARNING,
            message="trading calendar check recovered",
            occurred_at=boundary,
            context={"trading_date": local_day.isoformat()},
        )
        if not is_trading_day:
            return ()

        results: list[DailyRunResult] = []
        for item in planned:
            task = tasks.get(item.job_type)
            if task is None:
                self._record_job_alert(
                    item,
                    active=True,
                    message="scheduled daily task is not configured",
                    occurred_at=boundary,
                )
                raise DailyScheduleError(f"missing task for {item.job_type.value}")
            run_id = self.ledger.ensure_job_run(item.job_type, item.scheduled_for)

            def gated_task(task: JobTask = task, run_id: str = run_id) -> dict[str, object]:
                health = self.health.check(
                    probes,
                    checked_at=boundary,
                    source_job_run_id=run_id,
                )
                health.require_ready()
                return {
                    "health_check_ids": [check.check_id for check in health.checks],
                    "task_result": task(),
                }

            try:
                run = self.jobs.run(
                    item.job_type,
                    scheduled_for=item.scheduled_for,
                    task=gated_task,
                )
            except Exception as error:
                self._record_job_alert(
                    item,
                    active=True,
                    message=f"{type(error).__name__}: {error}",
                    occurred_at=boundary,
                    run_id=run_id,
                )
                raise
            if run.status is JobStatus.SUCCEEDED:
                self._record_job_alert(
                    item,
                    active=False,
                    message="scheduled daily job recovered",
                    occurred_at=boundary,
                    run_id=run_id,
                )
            results.append(DailyRunResult(planned=item, run=run))
        return tuple(results)

    def _record_job_alert(
        self,
        planned: ScheduledRun,
        *,
        active: bool,
        message: str,
        occurred_at: datetime,
        run_id: str | None = None,
    ) -> None:
        self.ledger.transition_alert(
            alert_key=(
                f"job:{planned.job_type.value}:"
                f"{planned.scheduled_for.astimezone(SHANGHAI).date().isoformat()}"
            ),
            active=active,
            severity=AlertSeverity.CRITICAL if active else AlertSeverity.WARNING,
            message=message,
            occurred_at=occurred_at,
            source_job_run_id=run_id,
            context={
                "job_type": planned.job_type.value,
                "scheduled_for": planned.scheduled_for.isoformat(),
            },
        )
