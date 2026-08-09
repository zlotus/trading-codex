from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from trading_codex.ledger.models import JobRunView, JobStatus, JobType, LedgerConflictError
from trading_codex.ledger.store import SQLiteLedger

JobTask = Callable[[], dict[str, object]]


class RetryableDailyJobs:
    """Runs daily jobs behind append-only start/outcome attempt events."""

    def __init__(
        self, ledger: SQLiteLedger, *, attempt_lease: timedelta = timedelta(minutes=30)
    ) -> None:
        if attempt_lease <= timedelta(0):
            raise ValueError("attempt_lease must be positive")
        self.ledger = ledger
        self.attempt_lease = attempt_lease

    def run_eod_preparation(
        self, *, scheduled_for: datetime, task: JobTask
    ) -> JobRunView:
        return self._run(JobType.EOD_PREPARATION, scheduled_for=scheduled_for, task=task)

    def run_opening_decision(
        self, *, scheduled_for: datetime, task: JobTask
    ) -> JobRunView:
        return self._run(JobType.OPENING_DECISION, scheduled_for=scheduled_for, task=task)

    def run(
        self,
        job_type: JobType,
        *,
        scheduled_for: datetime,
        task: JobTask,
    ) -> JobRunView:
        if not isinstance(job_type, JobType):
            raise ValueError("unsupported daily job type")
        return self._run(job_type, scheduled_for=scheduled_for, task=task)

    def _run(
        self,
        job_type: JobType,
        *,
        scheduled_for: datetime,
        task: JobTask,
    ) -> JobRunView:
        run_id = self.ledger.ensure_job_run(job_type, scheduled_for)
        current = self.ledger.job_run(run_id)
        if current.status is JobStatus.SUCCEEDED:
            return current

        attempt_id = uuid4().hex
        try:
            started = self.ledger.begin_job_attempt(
                run_id=run_id,
                attempt_id=attempt_id,
                occurred_at=datetime.now(UTC),
                stale_after=self.attempt_lease,
            )
        except LedgerConflictError:
            active = self.ledger.job_run(run_id)
            if active.status is JobStatus.RUNNING:
                return active
            raise
        if not started:
            return self.ledger.job_run(run_id)
        try:
            result = task()
            if not isinstance(result, dict):
                raise TypeError("daily job task must return a dictionary")
            self.ledger.record_job_attempt_event(
                run_id=run_id,
                attempt_id=attempt_id,
                phase="succeeded",
                occurred_at=datetime.now(UTC),
                result=result,
            )
        except Exception as error:
            self.ledger.record_job_attempt_event(
                run_id=run_id,
                attempt_id=attempt_id,
                phase="failed",
                occurred_at=datetime.now(UTC),
                error=f"{type(error).__name__}: {error}",
            )
            raise
        return self.ledger.job_run(run_id)
