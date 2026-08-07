from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rqalpha.const import EXCHANGE, INSTRUMENT_TYPE, MARKET, TRADING_CALENDAR_TYPE
from rqalpha.interface import AbstractDataSource, ExchangeRate
from rqalpha.model.instrument import Instrument

from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.time import require_aware

BAR_DTYPE = np.dtype(
    [
        ("datetime", "<u8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("prev_close", "<f8"),
        ("limit_up", "<f8"),
        ("limit_down", "<f8"),
        ("volume", "<f8"),
        ("total_turnover", "<f8"),
    ]
)
DIVIDEND_DTYPE = np.dtype(
    [
        ("book_closure_date", "<i8"),
        ("announcement_date", "<i8"),
        ("dividend_cash_before_tax", "<f8"),
        ("ex_dividend_date", "<i8"),
        ("payable_date", "<i8"),
        ("round_lot", "<f8"),
    ]
)
SPLIT_DTYPE = np.dtype([("ex_date", "<i8"), ("split_factor", "<f8")])


class RQAlphaParquetDataSource(AbstractDataSource):
    """Bounded RQAlpha data-source spike over normalized local Parquet data."""

    def __init__(self, normalized_root: Path, *, as_of: datetime) -> None:
        self.as_of = require_aware(as_of, field="as_of")
        self.store = ParquetDataStore(normalized_root)
        self._calendar = self._load_calendar()
        self._instruments = self._load_instruments()
        self._bars, self._bar_metadata = self._load_bars()
        self._corporate_actions = self.store.rows_as_of(
            "corporate_actions", as_of=self.as_of
        )

    def get_instruments(
        self,
        id_or_syms: Iterable[str] | None = None,
        types: Iterable[INSTRUMENT_TYPE] | None = None,
    ) -> Iterable[Instrument]:
        instruments = list(self._instruments.values())
        if id_or_syms is not None:
            accepted = set(id_or_syms)
            instruments = [
                instrument
                for instrument in instruments
                if instrument.order_book_id in accepted or instrument.symbol in accepted
            ]
        if types is not None:
            accepted_types = set(types)
            instruments = [
                instrument for instrument in instruments if instrument.type in accepted_types
            ]
        return instruments

    def get_trading_calendars(self) -> dict[TRADING_CALENDAR_TYPE, pd.DatetimeIndex]:
        return {TRADING_CALENDAR_TYPE.CN_STOCK: self._calendar}

    def available_data_range(self, frequency: str) -> tuple[date, date]:
        if frequency not in {"1d", "1w"}:
            raise NotImplementedError(f"unsupported RQAlpha frequency: {frequency}")
        if self._calendar.empty:
            raise ValueError("normalized trade calendar is empty")
        return self._calendar[0].date(), self._calendar[-1].date()

    def get_bar(self, instrument: Instrument, dt: datetime, frequency: str) -> np.void | None:
        if frequency != "1d":
            raise NotImplementedError(f"unsupported RQAlpha frequency: {frequency}")
        bars = self._bars.get(instrument.order_book_id)
        if bars is None:
            return None
        target = _date_int(_as_date(dt))
        index = bars["datetime"].searchsorted(target, side="left")
        if index >= len(bars) or bars[index]["datetime"] != target:
            return None
        return bars[index]

    def history_bars(
        self,
        instrument: Instrument,
        bar_count: int | None,
        frequency: str,
        fields: str | list[str] | None,
        dt: datetime,
        skip_suspended: bool = True,
        include_now: bool = False,
        adjust_type: str = "pre",
        adjust_orig: datetime | None = None,
    ) -> np.ndarray:
        del include_now, adjust_orig
        if frequency != "1d":
            raise NotImplementedError(f"unsupported RQAlpha frequency: {frequency}")
        if adjust_type not in {"none", "pre"}:
            raise NotImplementedError(f"unsupported adjustment type: {adjust_type}")
        bars = self._bars.get(instrument.order_book_id, np.empty(0, dtype=BAR_DTYPE))
        right = bars["datetime"].searchsorted(_date_int(_as_date(dt)), side="right")
        selected = bars[:right]
        if skip_suspended:
            selected = selected[~np.isnan(selected["close"])]
        if bar_count is not None:
            selected = selected[-bar_count:]
        if fields is None:
            return selected
        requested = [fields] if isinstance(fields, str) else fields
        unknown = set(requested) - set(BAR_DTYPE.names or ())
        if unknown:
            raise ValueError(f"unknown bar fields: {sorted(unknown)}")
        return selected[fields]

    def is_suspended(self, order_book_id: str, dates: Sequence[Any]) -> list[bool]:
        metadata = self._bar_metadata.get(order_book_id, {})
        return [not metadata.get(_as_date(value), {}).get("trade_status", False) for value in dates]

    def is_st_stock(self, order_book_id: str, dates: Sequence[Any]) -> list[bool]:
        metadata = self._bar_metadata.get(order_book_id, {})
        return [bool(metadata.get(_as_date(value), {}).get("is_st", False)) for value in dates]

    def get_dividend(self, instrument: Instrument) -> np.ndarray | None:
        actions = self._actions_for(instrument.order_book_id)
        dividends = []
        for action in actions:
            cash = float(action["cash_before_tax_per_share"])
            if cash <= 0:
                continue
            dividends.append(
                (
                    _plain_date_int(action["record_date"] or action["ex_date"]),
                    _plain_date_int(action["announcement_date"]),
                    cash,
                    _plain_date_int(action["ex_date"]),
                    _plain_date_int(action["pay_date"] or action["ex_date"]),
                    1.0,
                )
            )
        return np.array(dividends, dtype=DIVIDEND_DTYPE) if dividends else None

    def get_split(self, instrument: Instrument) -> np.ndarray | None:
        splits = []
        for action in self._actions_for(instrument.order_book_id):
            ratio = (
                Decimal(1)
                + action["stock_dividend_ratio"]
                + action["capitalization_ratio"]
            )
            if ratio == 1:
                continue
            splits.append((_date_int(action["ex_date"]), float(ratio)))
        return np.array(splits, dtype=SPLIT_DTYPE) if splits else None

    def get_yield_curve(
        self, start_date: Any, end_date: Any, tenor: list[str] | None = None
    ) -> pd.DataFrame:
        columns = tenor or ["0S", "1M", "3M", "6M", "1Y", "3Y", "5Y", "10Y"]
        index = pd.date_range(_as_date(start_date), _as_date(end_date), freq="D")
        return pd.DataFrame(0.02, index=index, columns=columns)

    def get_open_auction_bar(self, instrument: Instrument, dt: datetime) -> dict[str, float] | None:
        bar = self.get_bar(instrument, dt, "1d")
        if bar is None:
            return None
        return {
            "datetime": int(bar["datetime"]),
            "open": float(bar["open"]),
            "last": float(bar["open"]),
            "limit_up": float(bar["limit_up"]),
            "limit_down": float(bar["limit_down"]),
            "volume": 0.0,
            "total_turnover": 0.0,
        }

    def get_open_auction_volume(self, instrument: Instrument, dt: datetime) -> float:
        del instrument, dt
        return 0.0

    def get_exchange_rate(
        self,
        trading_date: date,
        local: MARKET,
        settlement: MARKET = MARKET.CN,
    ) -> ExchangeRate:
        del trading_date, local, settlement
        return ExchangeRate(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)

    def get_trading_minutes_for(self, instrument: Instrument, trading_dt: datetime) -> None:
        del instrument, trading_dt
        return None

    def get_settle_price(self, instrument: Instrument, day: date) -> float:
        del instrument, day
        return float("nan")

    def history_ticks(self, instrument: Instrument, count: int, dt: datetime) -> list[Any]:
        del instrument, count, dt
        raise NotImplementedError("tick data is outside the Milestone 1 spike")

    def current_snapshot(self, instrument: Instrument, frequency: str, dt: datetime) -> Any:
        del instrument, frequency, dt
        raise NotImplementedError("snapshots are outside the Milestone 1 spike")

    def get_futures_trading_parameters(self, instrument: Instrument, dt: datetime) -> Any:
        del instrument, dt
        raise NotImplementedError("futures are outside the Milestone 1 spike")

    def get_merge_ticks(
        self, order_book_id_list: list[str], trading_date: date, last_dt: datetime | None = None
    ) -> Iterable[Any]:
        del order_book_id_list, trading_date, last_dt
        return ()

    def get_share_transformation(self, order_book_id: str) -> None:
        del order_book_id
        return None

    def get_algo_bar(
        self, id_or_ins: str | Instrument, start_min: int, end_min: int, dt: datetime
    ) -> None:
        del id_or_ins, start_min, end_min, dt
        return None

    def _load_calendar(self) -> pd.DatetimeIndex:
        rows = self.store.rows_as_of("trade_calendar", as_of=self.as_of)
        dates = [row["calendar_date"] for row in rows if row["is_trading_day"]]
        return pd.DatetimeIndex(sorted(dates))

    def _load_instruments(self) -> dict[str, Instrument]:
        result = {}
        for row in self.store.rows_as_of("instruments", as_of=self.as_of):
            if row["security_type"] != "1":
                continue
            order_book_id, exchange, board = _instrument_identity(row["code"])
            out_date = row["out_date"] or date(2099, 12, 31)
            instrument = Instrument(
                {
                    "order_book_id": order_book_id,
                    "symbol": row["name"],
                    "type": "CS",
                    "exchange": exchange.value,
                    "listed_date": datetime.combine(row["ipo_date"], datetime.min.time()),
                    "de_listed_date": datetime.combine(out_date, datetime.min.time()),
                    "round_lot": 200 if board == "KSH" else 100,
                    "board_type": board,
                    "market_tplus": 1,
                    "status": "Active" if row["status"] == "1" else "Delisted",
                    "special_type": "Normal",
                },
                MARKET.CN,
            )
            result[order_book_id] = instrument
        return result

    def _load_bars(
        self,
    ) -> tuple[dict[str, np.ndarray], dict[str, dict[date, dict[str, bool]]]]:
        records: dict[str, list[tuple[Any, ...]]] = {}
        metadata: dict[str, dict[date, dict[str, bool]]] = {}
        for row in self.store.rows_as_of("daily_bars", as_of=self.as_of):
            if row["adjustment_flag"] != "3":
                continue
            order_book_id = baostock_to_order_book_id(row["code"])
            instrument = self._instruments.get(order_book_id)
            if instrument is None:
                continue
            price_limit = _price_limit_ratio(instrument, row)
            previous_close = row["previous_close"]
            limit_up = _limit_price(previous_close, price_limit) if previous_close else np.nan
            limit_down = _limit_price(previous_close, -price_limit) if previous_close else np.nan
            records.setdefault(order_book_id, []).append(
                (
                    _date_int(row["trade_date"]),
                    _float_or_nan(row["open"]),
                    _float_or_nan(row["high"]),
                    _float_or_nan(row["low"]),
                    _float_or_nan(row["close"]),
                    _float_or_nan(previous_close),
                    limit_up,
                    limit_down,
                    float(row["volume"]),
                    _float_or_nan(row["amount"]),
                )
            )
            metadata.setdefault(order_book_id, {})[row["trade_date"]] = {
                "trade_status": row["trade_status"],
                "is_st": row["is_st"],
            }
        arrays = {
            order_book_id: np.array(sorted(values), dtype=BAR_DTYPE)
            for order_book_id, values in records.items()
        }
        return arrays, metadata

    def _actions_for(self, order_book_id: str) -> list[dict[str, Any]]:
        code = order_book_id_to_baostock(order_book_id)
        return sorted(
            (row for row in self._corporate_actions if row["code"] == code),
            key=lambda row: row["ex_date"],
        )


def baostock_to_order_book_id(code: str) -> str:
    exchange, number = code.split(".", maxsplit=1)
    suffix = {"sh": "XSHG", "sz": "XSHE", "bj": "XBEI"}.get(exchange)
    if suffix is None:
        raise ValueError(f"unsupported BaoStock exchange: {exchange}")
    return f"{number}.{suffix}"


def order_book_id_to_baostock(order_book_id: str) -> str:
    number, suffix = order_book_id.split(".", maxsplit=1)
    prefix = {"XSHG": "sh", "XSHE": "sz", "XBEI": "bj"}.get(suffix)
    if prefix is None:
        raise ValueError(f"unsupported RQAlpha exchange suffix: {suffix}")
    return f"{prefix}.{number}"


def _instrument_identity(code: str) -> tuple[str, EXCHANGE, str]:
    order_book_id = baostock_to_order_book_id(code)
    if code.startswith("sh.688"):
        return order_book_id, EXCHANGE.XSHG, "KSH"
    if code.startswith(("sz.300", "sz.301")):
        return order_book_id, EXCHANGE.XSHE, "GEM"
    if code.startswith("bj."):
        return order_book_id, EXCHANGE.BJSE, "BJS"
    exchange = EXCHANGE.XSHG if code.startswith("sh.") else EXCHANGE.XSHE
    return order_book_id, exchange, "MainBoard"


def _price_limit_ratio(instrument: Instrument, row: dict[str, Any]) -> Decimal:
    if row["is_st"]:
        return Decimal("0.05")
    if instrument.board_type in {"KSH", "GEM"}:
        return Decimal("0.20")
    if instrument.board_type == "BJS":
        return Decimal("0.30")
    return Decimal("0.10")


def _limit_price(previous_close: Decimal, ratio: Decimal) -> float:
    price = previous_close * (Decimal(1) + ratio)
    return float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _float_or_nan(value: Decimal | None) -> float:
    return float(value) if value is not None else np.nan


def _date_int(value: date) -> int:
    return int(f"{value:%Y%m%d}000000")


def _plain_date_int(value: date) -> int:
    return int(f"{value:%Y%m%d}")


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, np.integer)):
        return datetime.strptime(str(value)[:8], "%Y%m%d").date()
    return pd.Timestamp(value).date()
