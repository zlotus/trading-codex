from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from trading_codex.domain.contracts import (
    RiskDecision,
    RiskRejection,
    TargetPortfolio,
    TargetWeight,
)
from trading_codex.domain.models import (
    DailyBar,
    DecisionSnapshot,
    RiskValidationError,
    SnapshotValidationError,
    StaleMarketDataError,
)
from trading_codex.portfolio.valuation import current_weight, portfolio_equity

RISK_VERSION = "a-share-hard-risk-v1"


@dataclass(frozen=True)
class RiskConfig:
    max_data_age: timedelta = timedelta(days=4)
    max_position_price_age: timedelta = timedelta(days=7)
    max_position_weight: Decimal = Decimal("0.20")
    max_gross_exposure: Decimal = Decimal("0.95")
    allow_st_buys: bool = False
    version: str = RISK_VERSION

    def __post_init__(self) -> None:
        if self.max_data_age <= timedelta(0):
            raise ValueError("max data age must be positive")
        if self.max_position_price_age <= timedelta(0):
            raise ValueError("max position price age must be positive")
        if not Decimal(0) < self.max_position_weight <= Decimal(1):
            raise ValueError("max position weight must be in (0, 1]")
        if not Decimal(0) <= self.max_gross_exposure <= Decimal(1):
            raise ValueError("max gross exposure must be in [0, 1]")
        if not self.version:
            raise ValueError("risk version is required")


class HardRiskEngine:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    @property
    def version(self) -> str:
        return self.config.version

    def validate_snapshot(self, snapshot: DecisionSnapshot) -> None:
        for code in snapshot.candidate_codes:
            current = snapshot.state_on(code, snapshot.decision_date)
            if current is None:
                raise SnapshotValidationError(
                    f"candidate {code} is missing its decision-date market state"
                )
            age = snapshot.as_of - current.available_at
            if age > self.config.max_data_age:
                raise StaleMarketDataError(
                    f"candidate {code} market state is stale by {age}"
                )
        for position in snapshot.positions:
            if position.quantity == 0:
                continue
            priced = snapshot.latest_priced_bar(position.code)
            if priced is None:
                raise SnapshotValidationError(
                    f"position {position.code} has no valuation bar"
                )
            age = snapshot.as_of - priced.available_at
            if age > self.config.max_position_price_age:
                raise StaleMarketDataError(
                    f"position {position.code} valuation is stale by {age}"
                )

    def evaluate(
        self,
        snapshot: DecisionSnapshot,
        target: TargetPortfolio,
    ) -> RiskDecision:
        self.validate_snapshot(snapshot)
        self._validate_target(snapshot, target)
        equity = portfolio_equity(snapshot)
        requested = {item.code: item for item in target.weights}
        codes = sorted(set(requested) | {position.code for position in snapshot.positions})
        approved: list[TargetWeight] = []
        rejections: list[RiskRejection] = []

        for code in codes:
            item = requested.get(code, TargetWeight(code=code, weight=Decimal(0), rank=10_000))
            existing_weight = current_weight(snapshot, code, equity)
            blocker: str | None = None
            if item.weight > existing_weight:
                blocker = self._buy_blocker(snapshot, code)
            elif item.weight < existing_weight:
                blocker = self._sell_blocker(snapshot, code)

            approved_weight = existing_weight if blocker is not None else item.weight
            if blocker is not None:
                rejections.append(
                    RiskRejection(
                        code=code,
                        reason=blocker,
                        requested_weight=item.weight,
                        approved_weight=approved_weight,
                    )
                )
            if approved_weight > 0:
                approved.append(
                    TargetWeight(code=code, weight=approved_weight, rank=item.rank)
                )

        return RiskDecision(
            snapshot_id=snapshot.snapshot_id,
            version=self.version,
            equity=equity,
            requested=target,
            approved_weights=tuple(sorted(approved, key=lambda item: item.code)),
            rejections=tuple(sorted(rejections, key=lambda item: item.code)),
        )

    def _validate_target(
        self,
        snapshot: DecisionSnapshot,
        target: TargetPortfolio,
    ) -> None:
        if target.snapshot_id != snapshot.snapshot_id:
            raise RiskValidationError("target portfolio belongs to a different snapshot")
        codes = [item.code for item in target.weights]
        if len(codes) != len(set(codes)):
            raise RiskValidationError("target portfolio contains duplicate instruments")
        for item in target.weights:
            if item.weight <= 0:
                raise RiskValidationError("target weights must be positive")
            if item.weight > self.config.max_position_weight:
                raise RiskValidationError(
                    f"target weight exceeds hard limit for {item.code}"
                )
            snapshot.rule_for(item.code)
        gross = sum((item.weight for item in target.weights), Decimal(0))
        if gross > self.config.max_gross_exposure:
            raise RiskValidationError("target gross exposure exceeds hard limit")
        if target.cash_weight != Decimal(1) - gross:
            raise RiskValidationError("target cash weight is internally inconsistent")

    def _buy_blocker(self, snapshot: DecisionSnapshot, code: str) -> str | None:
        state = snapshot.state_on(code, snapshot.decision_date)
        if state is None or not state.trade_status or state.volume <= 0:
            return "suspended"
        if state.is_st and not self.config.allow_st_buys:
            return "st_buy_blocked"
        ratio = Decimal("0.05") if state.is_st else snapshot.rule_for(code).price_limit_ratio
        if _at_limit(state, ratio, upper=True):
            return "limit_up"
        return None

    def _sell_blocker(self, snapshot: DecisionSnapshot, code: str) -> str | None:
        state = snapshot.state_on(code, snapshot.decision_date)
        if state is None or not state.trade_status or state.volume <= 0:
            return "suspended"
        ratio = Decimal("0.05") if state.is_st else snapshot.rule_for(code).price_limit_ratio
        if _at_limit(state, ratio, upper=False):
            return "limit_down"
        position = snapshot.position_for(code)
        if position is not None and position.quantity > 0 and position.sellable_quantity == 0:
            return "t_plus_one"
        return None


def _at_limit(state: DailyBar, ratio: Decimal, *, upper: bool) -> bool:
    if state.execution_close is None or state.previous_close is None:
        return True
    direction = Decimal(1) + ratio if upper else Decimal(1) - ratio
    limit = (state.previous_close * direction).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return state.execution_close >= limit if upper else state.execution_close <= limit
