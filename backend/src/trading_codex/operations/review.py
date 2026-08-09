from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from trading_codex.domain.hashing import canonical_sha256
from trading_codex.ledger.models import ForwardObservation

FORWARD_REVIEW_VERSION = "forward-attribution-review-v1"
MINIMUM_FORWARD_TRADING_DAYS = 60


class ObservationWindowError(RuntimeError):
    """Forward observations are incomplete or cannot support an attribution report."""


@dataclass(frozen=True)
class ReturnSeries:
    benchmark: Decimal
    base_target: Decimal
    base_simulated: Decimal
    ai_shadow: Decimal
    actual: Decimal


@dataclass(frozen=True)
class AttributionEffects:
    strategy_vs_benchmark: Decimal
    simulated_execution_vs_target: Decimal
    ai_overlay_vs_base_simulation: Decimal
    manual_execution_vs_base_simulation: Decimal


@dataclass(frozen=True)
class ObservationTrace:
    trading_date: date
    observation_id: str
    base_decision_id: str
    ai_shadow_decision_id: str
    snapshot_id: str
    metric_payload_sha256: str
    source_payloads: tuple[str, ...]


@dataclass(frozen=True)
class ForwardReviewReport:
    report_id: str
    version: str
    start_date: date
    end_date: date
    trading_days: int
    minimum_trading_days: int
    cumulative_returns: ReturnSeries
    attribution: AttributionEffects
    average_transaction_cost_rate: Decimal
    base_configuration_ids: tuple[str, ...]
    ai_shadow_configuration_ids: tuple[str, ...]
    traces: tuple[ObservationTrace, ...]

    def as_dict(self) -> dict[str, object]:
        return _jsonable(asdict(self))


class ForwardReviewBuilder:
    def __init__(
        self, *, minimum_trading_days: int = MINIMUM_FORWARD_TRADING_DAYS
    ) -> None:
        if minimum_trading_days < MINIMUM_FORWARD_TRADING_DAYS:
            raise ValueError(
                "minimum_trading_days must be at least "
                f"{MINIMUM_FORWARD_TRADING_DAYS}"
            )
        self.minimum_trading_days = minimum_trading_days

    def build(self, observations: tuple[ForwardObservation, ...]) -> ForwardReviewReport:
        ordered = tuple(sorted(observations, key=lambda item: item.trading_date))
        dates = tuple(item.trading_date for item in ordered)
        if len(dates) != len(set(dates)):
            raise ObservationWindowError("forward observations contain duplicate trading dates")
        if len(ordered) < self.minimum_trading_days:
            raise ObservationWindowError(
                "forward review requires at least "
                f"{self.minimum_trading_days} trading days; found {len(ordered)}"
            )
        if not ordered:
            raise ObservationWindowError("forward observations are empty")

        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            returns = ReturnSeries(
                benchmark=_compound(item.benchmark_return for item in ordered),
                base_target=_compound(item.base_target_return for item in ordered),
                base_simulated=_compound(item.base_simulated_return for item in ordered),
                ai_shadow=_compound(item.ai_shadow_return for item in ordered),
                actual=_compound(item.actual_return for item in ordered),
            )
            attribution = AttributionEffects(
                strategy_vs_benchmark=returns.base_target - returns.benchmark,
                simulated_execution_vs_target=returns.base_simulated - returns.base_target,
                ai_overlay_vs_base_simulation=returns.ai_shadow - returns.base_simulated,
                manual_execution_vs_base_simulation=returns.actual - returns.base_simulated,
            )
            average_cost = sum(
                (item.transaction_cost_rate for item in ordered), Decimal(0)
            ) / Decimal(len(ordered))
        traces = tuple(
            ObservationTrace(
                trading_date=item.trading_date,
                observation_id=item.observation_id,
                base_decision_id=item.base_decision_id,
                ai_shadow_decision_id=item.ai_shadow_decision_id,
                snapshot_id=item.snapshot_id,
                metric_payload_sha256=item.metric_payload_sha256,
                source_payloads=item.source_payloads,
            )
            for item in ordered
        )
        payload = {
            "version": FORWARD_REVIEW_VERSION,
            "start_date": dates[0],
            "end_date": dates[-1],
            "trading_days": len(ordered),
            "minimum_trading_days": self.minimum_trading_days,
            "cumulative_returns": returns,
            "attribution": attribution,
            "average_transaction_cost_rate": average_cost,
            "base_configuration_ids": tuple(
                sorted({item.base_configuration_id for item in ordered})
            ),
            "ai_shadow_configuration_ids": tuple(
                sorted({item.ai_shadow_configuration_id for item in ordered})
            ),
            "traces": traces,
        }
        return ForwardReviewReport(report_id=canonical_sha256(payload), **payload)


def write_forward_review(report: ForwardReviewReport, directory: Path) -> Path:
    payload = (
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"forward-review-{report.report_id[:16]}.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".forward-review-", suffix=".tmp", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ObservationWindowError("forward review artifact is not immutable") from None
        temporary_path.unlink()
        temporary_path = None
        return path
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _compound(values: Iterable[Decimal]) -> Decimal:
    wealth = Decimal(1)
    for value in values:
        wealth *= Decimal(1) + value
    return wealth - Decimal(1)


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"unsupported forward review value: {type(value).__name__}")
