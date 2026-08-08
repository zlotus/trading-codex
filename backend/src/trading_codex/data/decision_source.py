from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.domain.models import (
    SHANGHAI,
    DailyBar,
    DecisionSnapshot,
    InstrumentRule,
    PortfolioPosition,
    SnapshotValidationError,
    aware_utc,
)

SIGNAL_ADJUSTMENT_FLAG = "2"
EXECUTION_ADJUSTMENT_FLAG = "3"


class ParquetDecisionSnapshotSource:
    """Build immutable point-in-time decisions from normalized local datasets."""

    def __init__(self, store: ParquetDataStore) -> None:
        self.store = store

    def build(
        self,
        *,
        as_of: datetime,
        history_start: date,
        decision_date: date,
        execution_deadline: datetime,
        cash: Decimal,
        candidate_codes: Iterable[str],
        positions: Iterable[PortfolioPosition] = (),
        rules: Iterable[InstrumentRule],
    ) -> DecisionSnapshot:
        boundary = aware_utc(as_of, field="as_of")
        if history_start > decision_date:
            raise SnapshotValidationError("history_start must not follow decision_date")
        if decision_date > boundary.astimezone(SHANGHAI).date():
            raise SnapshotValidationError("decision_date exceeds as_of")

        candidates = tuple(sorted(set(candidate_codes)))
        position_items = _sorted_unique(positions, item_name="position")
        rule_items = _sorted_unique(rules, item_name="instrument rule")
        required_codes = set(candidates) | {position.code for position in position_items}

        calendar_rows = [
            row
            for row in self.store.rows_as_of("trade_calendar", as_of=boundary)
            if history_start <= row["calendar_date"] <= decision_date
        ]
        calendar_by_date = {row["calendar_date"]: row for row in calendar_rows}
        expected_calendar_dates = set(_date_range(history_start, decision_date))
        missing_calendar = sorted(expected_calendar_dates - set(calendar_by_date))
        if missing_calendar:
            raise SnapshotValidationError(
                f"trade calendar is incomplete: {_sample_dates(missing_calendar)}"
            )
        if not calendar_by_date[decision_date]["is_trading_day"]:
            raise SnapshotValidationError("decision_date is not a trading day")
        trading_days = {
            day for day, row in calendar_by_date.items() if row["is_trading_day"]
        }

        universe_rows = [
            row
            for row in self.store.rows_as_of("historical_universe", as_of=boundary)
            if row["snapshot_date"] in trading_days
        ]
        universe_by_date: dict[date, dict[str, dict[str, Any]]] = defaultdict(dict)
        for row in universe_rows:
            universe_by_date[row["snapshot_date"]][row["code"]] = row
        missing_universe = sorted(trading_days - set(universe_by_date))
        if missing_universe:
            raise SnapshotValidationError(
                f"historical universe is incomplete: {_sample_dates(missing_universe)}"
            )
        missing_current_codes = sorted(
            required_codes - set(universe_by_date[decision_date])
        )
        if missing_current_codes:
            raise SnapshotValidationError(
                f"decision-date universe is missing instruments: {missing_current_codes}"
            )

        signal_rows = self.store.daily_bars(
            codes=required_codes,
            start_date=history_start,
            end_date=decision_date,
            as_of=boundary,
            adjustment_flag=SIGNAL_ADJUSTMENT_FLAG,
        )
        execution_rows = self.store.daily_bars(
            codes=required_codes,
            start_date=history_start,
            end_date=decision_date,
            as_of=boundary,
            adjustment_flag=EXECUTION_ADJUSTMENT_FLAG,
        )
        signal_by_key = {_bar_key(row): row for row in signal_rows}
        execution_by_key = {_bar_key(row): row for row in execution_rows}
        signal_keys = set(signal_by_key)
        execution_keys = set(execution_by_key)
        if signal_keys != execution_keys:
            missing_signal = sorted(execution_keys - signal_keys)
            missing_execution = sorted(signal_keys - execution_keys)
            raise SnapshotValidationError(
                "daily price tracks are incomplete: "
                f"missing adjustflag=2 {_sample_keys(missing_signal)}, "
                f"missing adjustflag=3 {_sample_keys(missing_execution)}"
            )

        expected_bar_keys = {
            (code, day)
            for day in trading_days
            for code, row in universe_by_date[day].items()
            if code in required_codes and row["trade_status"]
        }
        expected_bar_keys.update((code, decision_date) for code in required_codes)
        missing_bars = sorted(expected_bar_keys - signal_keys)
        if missing_bars:
            raise SnapshotValidationError(
                f"daily price tracks are missing required rows: {_sample_keys(missing_bars)}"
            )

        bars = tuple(
            self._daily_bar(signal_by_key[key], execution_by_key[key])
            for key in sorted(signal_keys)
        )
        payloads = {
            row["source_payload_sha256"]
            for row in [*calendar_rows, *universe_rows, *signal_rows, *execution_rows]
        }
        return DecisionSnapshot(
            as_of=boundary,
            decision_date=decision_date,
            execution_deadline=execution_deadline,
            cash=cash,
            candidate_codes=candidates,
            bars=bars,
            positions=position_items,
            rules=rule_items,
            source_payloads=tuple(sorted(payloads)),
        )

    @staticmethod
    def _daily_bar(
        signal: dict[str, Any], execution: dict[str, Any]
    ) -> DailyBar:
        for field in ("code", "trade_date", "volume", "trade_status", "is_st"):
            if signal[field] != execution[field]:
                key = _bar_key(signal)
                raise SnapshotValidationError(
                    f"daily price tracks disagree for {key[0]} on {key[1]}: {field}"
                )
        return DailyBar(
            code=execution["code"],
            trade_date=execution["trade_date"],
            signal_close=signal["close"],
            execution_close=execution["close"],
            previous_close=execution["previous_close"],
            volume=execution["volume"],
            trade_status=execution["trade_status"],
            is_st=execution["is_st"],
            available_at=max(signal["available_at"], execution["available_at"]),
        )


def _bar_key(row: dict[str, Any]) -> tuple[str, date]:
    return row["code"], row["trade_date"]


def _date_range(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _sample_dates(values: list[date]) -> str:
    return ", ".join(value.isoformat() for value in values[:5])


def _sample_keys(values: list[tuple[str, date]]) -> str:
    return str([(code, day.isoformat()) for code, day in values[:5]])


def _sorted_unique(
    values: Iterable[PortfolioPosition] | Iterable[InstrumentRule],
    *,
    item_name: str,
) -> tuple[PortfolioPosition, ...] | tuple[InstrumentRule, ...]:
    items = tuple(values)
    codes = [item.code for item in items]
    if len(codes) != len(set(codes)):
        raise SnapshotValidationError(f"duplicate {item_name} instruments")
    return tuple(sorted(items, key=lambda item: item.code))
