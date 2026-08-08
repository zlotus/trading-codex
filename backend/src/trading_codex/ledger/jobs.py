from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from trading_codex.ledger.models import JobRunView, JobStatus, JobType
from trading_codex.ledger.store import SQLiteLedger

JobTask = Callable[[], dict[str, object]]


class RetryableDailyJobs:
    """Runs daily jobs behind append-only start/outcome attempt events."""

    def __init__(self, ledger: SQLiteLedger) -> None:
        self.ledger = ledger

    def run_eod_preparation(
        self, *, scheduled_for: datetime, task: JobTask
    ) -> JobRunView:
        return self._run(JobType.EOD_PREPARATION, scheduled_for=scheduled_for, task=task)

    def run_opening_decision(
        self, *, scheduled_for: datetime, task: JobTask
    ) -> JobRunView:
        return self._run(JobType.OPENING_DECISION, scheduled_for=scheduled_for, task=task)

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
        self.ledger.record_job_attempt_event(
            run_id=run_id,
            attempt_id=attempt_id,
            phase="started",
            occurred_at=datetime.now(UTC),
        )
        try:
            result = task()
        except Exception as error:
            self.ledger.record_job_attempt_event(
                run_id=run_id,
                attempt_id=attempt_id,
                phase="failed",
                occurred_at=datetime.now(UTC),
                error=f"{type(error).__name__}: {error}",
            )
            raise
        self.ledger.record_job_attempt_event(
            run_id=run_id,
            attempt_id=attempt_id,
            phase="succeeded",
            occurred_at=datetime.now(UTC),
            result=result,
        )
        return self.ledger.job_run(run_id)
