from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from trading_codex.domain.models import DecisionSnapshot, SnapshotValidationError


def portfolio_equity(snapshot: DecisionSnapshot) -> Decimal:
    equity = snapshot.cash
    for position in snapshot.positions:
        if position.quantity == 0:
            continue
        bar = snapshot.latest_priced_bar(position.code)
        if bar is None or bar.execution_close is None:
            raise SnapshotValidationError(
                f"position {position.code} has no point-in-time valuation price"
            )
        equity += Decimal(position.quantity) * bar.execution_close
    if equity <= 0:
        raise SnapshotValidationError("portfolio equity must be positive")
    return equity


def current_weight(snapshot: DecisionSnapshot, code: str, equity: Decimal) -> Decimal:
    position = snapshot.position_for(code)
    if position is None or position.quantity == 0:
        return Decimal(0)
    bar = snapshot.latest_priced_bar(code)
    if bar is None or bar.execution_close is None:
        raise SnapshotValidationError(f"position {code} has no valuation price")
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        return Decimal(position.quantity) * bar.execution_close / equity
