from collections.abc import Iterable

from trading_codex.domain.contracts import AllocationState, ReplayResult
from trading_codex.domain.models import DecisionSnapshot, SnapshotValidationError
from trading_codex.domain.pipeline import DecisionPipeline
from trading_codex.portfolio.regime_allocation import allocation_state


class HistoricalReplay:
    """Run historical snapshots through the same pipeline used for one live decision."""

    def __init__(self, pipeline: DecisionPipeline) -> None:
        self.pipeline = pipeline

    def run(
        self,
        snapshots: Iterable[DecisionSnapshot],
        *,
        initial_allocation: AllocationState | None = None,
    ) -> ReplayResult:
        ordered = tuple(snapshots)
        as_ofs = tuple(snapshot.as_of for snapshot in ordered)
        if as_ofs != tuple(sorted(set(as_ofs))):
            raise SnapshotValidationError(
                "historical replay snapshots must have strictly increasing as_of values"
            )
        previous = initial_allocation
        runs = []
        for snapshot in ordered:
            run = self.pipeline.run(snapshot, previous_allocation=previous)
            runs.append(run)
            previous = allocation_state(snapshot.as_of, run.allocated)
        return ReplayResult(
            configuration_id=self.pipeline.configuration_id,
            runs=tuple(runs),
        )
