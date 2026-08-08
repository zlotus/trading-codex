from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
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

    def __post_init__(self) -> None:
        _validate_code(self.code)
        object.__setattr__(
            self, "available_at", aware_utc(self.available_at, field="bar.available_at")
        )
        if self.volume < 0:
            raise SnapshotValidationError("bar volume must be non-negative")
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
    data_version: str = "decision-snapshot-v1"

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

    @property
    def snapshot_id(self) -> str:
        return canonical_sha256(self)

    def bars_for(self, code: str) -> tuple[DailyBar, ...]:
        return tuple(bar for bar in self.bars if bar.code == code)

    def state_on(self, code: str, day: date) -> DailyBar | None:
        return next(
            (bar for bar in reversed(self.bars) if bar.code == code and bar.trade_date == day),
            None,
        )

    def latest_priced_bar(self, code: str) -> DailyBar | None:
        return next(
            (
                bar
                for bar in reversed(self.bars)
                if bar.code == code and bar.execution_close is not None
            ),
            None,
        )

    def position_for(self, code: str) -> PortfolioPosition | None:
        return next((position for position in self.positions if position.code == code), None)

    def rule_for(self, code: str) -> InstrumentRule:
        rule = next((item for item in self.rules if item.code == code), None)
        if rule is None:
            raise SnapshotValidationError(f"missing instrument rule for {code}")
        return rule

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
