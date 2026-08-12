from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from functools import cached_property
from zoneinfo import ZoneInfo

from trading_codex.domain.hashing import canonical_sha256

SHANGHAI = ZoneInfo("Asia/Shanghai")


class DecisionKernelError(RuntimeError):
    """Base error for deterministic decision evaluation."""


class SnapshotValidationError(DecisionKernelError):
    """A decision snapshot is incomplete or internally inconsistent."""


class StaleMarketDataError(DecisionKernelError):
    """Required point-in-time market data is too old for a decision."""


class RiskValidationError(DecisionKernelError):
    """A target portfolio violates a deterministic hard-risk invariant."""


class DecisionPoint(StrEnum):
    EOD = "eod"
    OPENING_0935 = "opening_0935"


def aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SnapshotValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class DailyBar:
    code: str
    trade_date: date
    signal_close: Decimal | None
    execution_close: Decimal | None
    previous_close: Decimal | None
    volume: int
    trade_status: bool
    is_st: bool
    available_at: datetime
    amount: Decimal | None = None
    turnover: Decimal | None = None

    def __post_init__(self) -> None:
        _validate_code(self.code)
        object.__setattr__(
            self, "available_at", aware_utc(self.available_at, field="bar.available_at")
        )
        if self.volume < 0:
            raise SnapshotValidationError("bar volume must be non-negative")
        if self.amount is not None and self.amount < 0:
            raise SnapshotValidationError("bar amount must be non-negative")
        if self.turnover is not None and self.turnover < 0:
            raise SnapshotValidationError("bar turnover must be non-negative")
        for field in ("signal_close", "execution_close", "previous_close"):
            value = getattr(self, field)
            if value is not None and value <= 0:
                raise SnapshotValidationError(f"bar {field} must be positive")
        if self.trade_status and any(
            value is None
            for value in (self.signal_close, self.execution_close, self.previous_close)
        ):
            raise SnapshotValidationError("tradable bars require signal and execution prices")


@dataclass(frozen=True)
class OpeningBar:
    code: str
    timestamp: datetime
    open_price: Decimal
    close_price: Decimal
    previous_close: Decimal
    volume: int
    amount: Decimal
    trade_status: bool
    is_st: bool
    available_at: datetime

    def __post_init__(self) -> None:
        _validate_code(self.code)
        object.__setattr__(self, "timestamp", aware_utc(self.timestamp, field="opening.timestamp"))
        object.__setattr__(
            self,
            "available_at",
            aware_utc(self.available_at, field="opening.available_at"),
        )
        if self.open_price <= 0 or self.close_price <= 0 or self.previous_close <= 0:
            raise SnapshotValidationError("opening prices must be positive")
        if self.volume < 0 or self.amount < 0:
            raise SnapshotValidationError("opening volume and amount must be non-negative")
        if self.available_at < self.timestamp:
            raise SnapshotValidationError("opening bar cannot be available before its timestamp")


@dataclass(frozen=True)
class PortfolioPosition:
    code: str
    quantity: int
    sellable_quantity: int
    average_cost: Decimal

    def __post_init__(self) -> None:
        _validate_code(self.code)
        if self.quantity < 0:
            raise SnapshotValidationError("position quantity must be non-negative")
        if not 0 <= self.sellable_quantity <= self.quantity:
            raise SnapshotValidationError("sellable quantity must be within total quantity")
        if self.average_cost < 0:
            raise SnapshotValidationError("average cost must be non-negative")


@dataclass(frozen=True)
class InstrumentRule:
    code: str
    lot_size: int
    price_limit_ratio: Decimal

    def __post_init__(self) -> None:
        _validate_code(self.code)
        if self.lot_size <= 0:
            raise SnapshotValidationError("lot size must be positive")
        if not Decimal(0) < self.price_limit_ratio < Decimal(1):
            raise SnapshotValidationError("price limit ratio must be between zero and one")


@dataclass(frozen=True)
class DecisionSnapshot:
    as_of: datetime
    decision_date: date
    execution_deadline: datetime
    cash: Decimal
    candidate_codes: tuple[str, ...]
    bars: tuple[DailyBar, ...]
    positions: tuple[PortfolioPosition, ...]
    rules: tuple[InstrumentRule, ...]
    source_payloads: tuple[str, ...]
    decision_point: DecisionPoint
    regime_codes: tuple[str, ...] = ()
    opening_bars: tuple[OpeningBar, ...] = ()
    data_version: str = "decision-snapshot-v2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", aware_utc(self.as_of, field="as_of"))
        object.__setattr__(
            self,
            "execution_deadline",
            aware_utc(self.execution_deadline, field="execution_deadline"),
        )
        if self.execution_deadline <= self.as_of:
            raise SnapshotValidationError("execution deadline must follow as_of")
        if self.decision_date > self.as_of.astimezone(SHANGHAI).date():
            raise SnapshotValidationError("decision date exceeds as_of")
        if self.cash < 0:
            raise SnapshotValidationError("cash must be non-negative")
        if not isinstance(self.decision_point, DecisionPoint):
            raise SnapshotValidationError("decision point is invalid")
        if not self.data_version:
            raise SnapshotValidationError("data version is required")
        self._validate_sorted_unique()
        rule_codes = {rule.code for rule in self.rules}
        required_rules = set(self.candidate_codes) | {
            position.code for position in self.positions
        }
        if not required_rules <= rule_codes:
            missing = sorted(required_rules - rule_codes)
            raise SnapshotValidationError(f"missing instrument rules: {missing}")
        for bar in self.bars:
            if bar.trade_date > self.decision_date:
                raise SnapshotValidationError("snapshot contains a future trade date")
            if bar.available_at > self.as_of:
                raise SnapshotValidationError("snapshot contains a bar unavailable at as_of")
        for bar in self.opening_bars:
            opening_time = bar.timestamp.astimezone(SHANGHAI)
            if opening_time.date() != self.decision_date:
                raise SnapshotValidationError("opening bar belongs to a different decision date")
            if (
                opening_time.hour,
                opening_time.minute,
                opening_time.second,
                opening_time.microsecond,
            ) != (9, 35, 0, 0):
                raise SnapshotValidationError("opening bar must use the exact 09:35 checkpoint")
            if bar.available_at > self.as_of:
                raise SnapshotValidationError(
                    "snapshot contains an opening bar unavailable at as_of"
                )
            opening_codes = (
                set(self.regime_codes)
                | set(self.candidate_codes)
                | {position.code for position in self.positions}
            )
            if bar.code not in opening_codes:
                raise SnapshotValidationError("opening bar is outside the decision universe")
            prior = next(
                (
                    daily
                    for daily in reversed(self.bars)
                    if daily.code == bar.code
                    and daily.trade_date < self.decision_date
                    and daily.execution_close is not None
                ),
                None,
            )
            if prior is None or prior.execution_close != bar.previous_close:
                raise SnapshotValidationError(
                    "opening previous close disagrees with completed daily data"
                )

    @cached_property
    def snapshot_id(self) -> str:
        return canonical_sha256(self)

    def bars_for(self, code: str) -> tuple[DailyBar, ...]:
        return self._bars_by_code.get(code, ())

    def state_on(self, code: str, day: date) -> DailyBar | None:
        return self._bars_by_code_and_date.get((code, day))

    def latest_priced_bar(self, code: str) -> DailyBar | None:
        current = self.decision_state(code)
        if current is not None and current.execution_close is not None:
            return current
        return next(
            (
                bar
                for bar in reversed(self.bars)
                if bar.code == code and bar.execution_close is not None
            ),
            None,
        )

    def decision_state(self, code: str) -> DailyBar | None:
        current = self.state_on(code, self.decision_date)
        if current is not None:
            return current
        opening = self.opening_bar_for(code)
        if opening is None:
            return None
        prior = next(
            (
                bar
                for bar in reversed(self.bars)
                if bar.code == code
                and bar.trade_date < self.decision_date
                and bar.signal_close is not None
            ),
            None,
        )
        if prior is None:
            return None
        return DailyBar(
            code=code,
            trade_date=self.decision_date,
            signal_close=prior.signal_close,
            execution_close=opening.close_price,
            previous_close=opening.previous_close,
            volume=opening.volume,
            trade_status=opening.trade_status,
            is_st=opening.is_st,
            available_at=opening.available_at,
            amount=opening.amount,
        )

    def position_for(self, code: str) -> PortfolioPosition | None:
        return self._positions_by_code.get(code)

    def opening_bar_for(self, code: str) -> OpeningBar | None:
        return self._opening_by_code.get(code)

    def rule_for(self, code: str) -> InstrumentRule:
        rule = self._rules_by_code.get(code)
        if rule is None:
            raise SnapshotValidationError(f"missing instrument rule for {code}")
        return rule

    @cached_property
    def _bars_by_code(self) -> dict[str, tuple[DailyBar, ...]]:
        grouped: dict[str, list[DailyBar]] = {}
        for bar in self.bars:
            grouped.setdefault(bar.code, []).append(bar)
        return {code: tuple(values) for code, values in grouped.items()}

    @cached_property
    def _bars_by_code_and_date(self) -> dict[tuple[str, date], DailyBar]:
        return {(bar.code, bar.trade_date): bar for bar in self.bars}

    @cached_property
    def _positions_by_code(self) -> dict[str, PortfolioPosition]:
        return {position.code: position for position in self.positions}

    @cached_property
    def _rules_by_code(self) -> dict[str, InstrumentRule]:
        return {rule.code: rule for rule in self.rules}

    @cached_property
    def _opening_by_code(self) -> dict[str, OpeningBar]:
        return {bar.code: bar for bar in self.opening_bars}

    def _validate_sorted_unique(self) -> None:
        candidate_codes = tuple(sorted(set(self.candidate_codes)))
        if self.candidate_codes != candidate_codes:
            raise SnapshotValidationError("candidate codes must be sorted and unique")
        bar_keys = tuple((bar.code, bar.trade_date) for bar in self.bars)
        if bar_keys != tuple(sorted(set(bar_keys))):
            raise SnapshotValidationError("bars must be sorted with unique code/date keys")
        position_codes = tuple(position.code for position in self.positions)
        if position_codes != tuple(sorted(set(position_codes))):
            raise SnapshotValidationError("positions must be sorted and unique")
        rule_codes = tuple(rule.code for rule in self.rules)
        if rule_codes != tuple(sorted(set(rule_codes))):
            raise SnapshotValidationError("rules must be sorted and unique")
        if self.source_payloads != tuple(sorted(set(self.source_payloads))):
            raise SnapshotValidationError("source payload hashes must be sorted and unique")
        regime_codes = tuple(sorted(set(self.regime_codes)))
        if self.regime_codes != regime_codes:
            raise SnapshotValidationError("regime codes must be sorted and unique")
        bar_codes = {bar.code for bar in self.bars}
        if not set(self.regime_codes) <= bar_codes:
            missing = sorted(set(self.regime_codes) - bar_codes)
            raise SnapshotValidationError(f"regime universe is missing daily bars: {missing}")
        opening_keys = tuple((bar.code, bar.timestamp) for bar in self.opening_bars)
        if opening_keys != tuple(sorted(set(opening_keys))):
            raise SnapshotValidationError(
                "opening bars must be sorted with unique code/timestamp keys"
            )
        if not self.source_payloads:
            raise SnapshotValidationError("at least one source payload hash is required")
        if any(
            len(payload) != 64
            or payload != payload.lower()
            or any(character not in "0123456789abcdef" for character in payload)
            for payload in self.source_payloads
        ):
            raise SnapshotValidationError("source payload hashes must be lowercase SHA-256 values")


def _validate_code(code: str) -> None:
    if not code or code != code.lower():
        raise SnapshotValidationError("instrument codes must be non-empty lowercase values")
