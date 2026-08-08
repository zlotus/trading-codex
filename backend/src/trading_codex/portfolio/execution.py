from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, localcontext

from trading_codex.domain.contracts import (
    ExecutionPlan,
    OrderSide,
    PlannedOrder,
    RiskDecision,
    TargetWeight,
)
from trading_codex.domain.models import DailyBar, DecisionSnapshot, RiskValidationError

EXECUTION_VERSION = "a-share-execution-plan-v1"
MONEY_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class ExecutionConfig:
    commission_rate: Decimal = Decimal("0.0003")
    minimum_commission: Decimal = Decimal("5")
    stamp_duty_rate: Decimal = Decimal("0.0005")
    transfer_fee_rate: Decimal = Decimal("0.00001")
    version: str = EXECUTION_VERSION

    def __post_init__(self) -> None:
        for name in ("commission_rate", "stamp_duty_rate", "transfer_fee_rate"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.minimum_commission < 0:
            raise ValueError("minimum commission must be non-negative")
        if not self.version:
            raise ValueError("execution version is required")


@dataclass(frozen=True)
class _OrderSpec:
    target: TargetWeight
    quantity: int
    price: Decimal


class ExecutionPlanner:
    def __init__(self, config: ExecutionConfig | None = None) -> None:
        self.config = config or ExecutionConfig()

    @property
    def version(self) -> str:
        return self.config.version

    def plan(self, snapshot: DecisionSnapshot, risk: RiskDecision) -> ExecutionPlan:
        if risk.snapshot_id != snapshot.snapshot_id:
            raise ValueError("risk decision belongs to a different snapshot")
        rejected = {item.code for item in risk.rejections}
        targets = {item.code: item for item in risk.approved_weights}
        codes = sorted(set(targets) | {position.code for position in snapshot.positions})
        sell_specs: list[_OrderSpec] = []
        buy_specs: list[_OrderSpec] = []

        for code in codes:
            if code in rejected:
                continue
            target = targets.get(
                code,
                TargetWeight(code=code, weight=Decimal(0), rank=10_000),
            )
            state = snapshot.decision_state(code)
            if not _tradable(state):
                continue
            assert state is not None and state.execution_close is not None
            position = snapshot.position_for(code)
            current_quantity = position.quantity if position is not None else 0
            rule = snapshot.rule_for(code)
            desired_quantity = _target_quantity(
                equity=risk.equity,
                weight=target.weight,
                price=state.execution_close,
                lot_size=rule.lot_size,
            )
            delta = desired_quantity - current_quantity
            if delta > 0 and not _at_limit(state, rule.price_limit_ratio, upper=True):
                quantity = delta - delta % rule.lot_size
                if quantity > 0:
                    buy_specs.append(_OrderSpec(target, quantity, state.execution_close))
            elif delta < 0 and not _at_limit(state, rule.price_limit_ratio, upper=False):
                sellable = position.sellable_quantity if position is not None else 0
                requested = min(-delta, sellable)
                quantity = (
                    requested
                    if desired_quantity == 0
                    else requested - requested % rule.lot_size
                )
                if quantity > 0:
                    sell_specs.append(_OrderSpec(target, quantity, state.execution_close))

        cash = snapshot.cash
        orders: list[PlannedOrder] = []
        for spec in sorted(sell_specs, key=lambda item: item.target.code):
            fees = self._fees(spec.price * spec.quantity, side=OrderSide.SELL)
            cash_after_sale = cash + spec.price * spec.quantity - fees
            if cash_after_sale < 0:
                raise RiskValidationError(
                    f"sell fees would produce negative cash for {spec.target.code}"
                )
            cash = cash_after_sale
            orders.append(self._order(snapshot, spec, OrderSide.SELL, fees))

        for spec in sorted(buy_specs, key=lambda item: (item.target.rank, item.target.code)):
            rule = snapshot.rule_for(spec.target.code)
            quantity = self._affordable_quantity(
                cash=cash,
                requested=spec.quantity,
                price=spec.price,
                lot_size=rule.lot_size,
            )
            if quantity == 0:
                continue
            affordable = _OrderSpec(spec.target, quantity, spec.price)
            fees = self._fees(spec.price * quantity, side=OrderSide.BUY)
            cash -= spec.price * quantity + fees
            orders.append(self._order(snapshot, affordable, OrderSide.BUY, fees))

        return ExecutionPlan(
            snapshot_id=snapshot.snapshot_id,
            version=self.version,
            orders=tuple(orders),
            estimated_cash_after_orders=cash.quantize(
                MONEY_QUANTUM, rounding=ROUND_HALF_UP
            ),
        )

    def _affordable_quantity(
        self,
        *,
        cash: Decimal,
        requested: int,
        price: Decimal,
        lot_size: int,
    ) -> int:
        maximum = int(cash // (price * lot_size)) * lot_size
        quantity = min(requested, maximum)
        while quantity > 0:
            notional = price * quantity
            if notional + self._fees(notional, side=OrderSide.BUY) <= cash:
                return quantity
            quantity -= lot_size
        return 0

    def _fees(self, notional: Decimal, *, side: OrderSide) -> Decimal:
        commission = max(
            notional * self.config.commission_rate,
            self.config.minimum_commission,
        )
        transfer = notional * self.config.transfer_fee_rate
        stamp = notional * self.config.stamp_duty_rate if side is OrderSide.SELL else 0
        return (commission + transfer + stamp).quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_UP
        )

    @staticmethod
    def _order(
        snapshot: DecisionSnapshot,
        spec: _OrderSpec,
        side: OrderSide,
        fees: Decimal,
    ) -> PlannedOrder:
        return PlannedOrder(
            code=spec.target.code,
            side=side,
            quantity=spec.quantity,
            reference_price=spec.price,
            estimated_fees=fees,
            target_weight=spec.target.weight,
            expires_at=snapshot.execution_deadline,
        )


def _target_quantity(
    *, equity: Decimal, weight: Decimal, price: Decimal, lot_size: int
) -> int:
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        raw = int((equity * weight / price).to_integral_value(rounding=ROUND_DOWN))
    return raw - raw % lot_size


def _tradable(state: DailyBar | None) -> bool:
    return bool(
        state is not None
        and state.trade_status
        and state.volume > 0
        and state.execution_close is not None
        and state.previous_close is not None
    )


def _at_limit(state: DailyBar, ratio: Decimal, *, upper: bool) -> bool:
    if state.execution_close is None or state.previous_close is None:
        return True
    effective_ratio = Decimal("0.05") if state.is_st else ratio
    direction = Decimal(1) + effective_ratio if upper else Decimal(1) - effective_ratio
    limit = (state.previous_close * direction).quantize(
        MONEY_QUANTUM, rounding=ROUND_HALF_UP
    )
    return state.execution_close >= limit if upper else state.execution_close <= limit
