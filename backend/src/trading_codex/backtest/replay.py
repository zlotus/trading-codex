from collections.abc import Iterable

from trading_codex.domain.contracts import ReplayResult
from trading_codex.domain.models import DecisionSnapshot, SnapshotValidationError
from trading_codex.domain.pipeline import DecisionPipeline


class HistoricalReplay:
    """Run historical snapshots through the same pipeline used for one live decision."""

    def __init__(self, pipeline: DecisionPipeline) -> None:
        self.pipeline = pipeline

    def run(self, snapshots: Iterable[DecisionSnapshot]) -> ReplayResult:
        ordered = tuple(snapshots)
        as_ofs = tuple(snapshot.as_of for snapshot in ordered)
        if as_ofs != tuple(sorted(set(as_ofs))):
            raise SnapshotValidationError(
                "historical replay snapshots must have strictly increasing as_of values"
            )
        return ReplayResult(
            configuration_id=self.pipeline.configuration_id,
            runs=tuple(self.pipeline.run(snapshot) for snapshot in ordered),
        )
