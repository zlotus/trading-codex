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
from trading_codex.data.time import SHANGHAI, require_aware

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
METADATA_DTYPE = np.dtype(
    [
        ("datetime", "<u8"),
        ("trade_status", "?"),
        ("is_st", "?"),
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

    def __init__(
        self,
        normalized_root: Path,
        *,
        as_of: datetime,
        codes: Iterable[str] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> None:
        self.as_of = require_aware(as_of, field="as_of")
        self.codes = tuple(sorted(set(codes or ())))
        self.start_date = start_date
        self.end_date = end_date
        if start_date is not None and end_date is not None and end_date < start_date:
            raise ValueError("RQAlpha adapter end_date must not precede start_date")
        if end_date is not None and end_date > self.as_of.astimezone(SHANGHAI).date():
            raise ValueError("RQAlpha adapter end_date exceeds as_of")
        self.store = ParquetDataStore(normalized_root)
        self._calendar = self._load_calendar()
        self._instruments = self._load_instruments()
        if self.codes:
            loaded_codes = {
                order_book_id_to_baostock(value) for value in self._instruments
            }
            if loaded_codes != set(self.codes):
                missing = sorted(set(self.codes) - loaded_codes)
                raise ValueError(f"RQAlpha adapter instruments are missing: {missing}")
        if not self.codes:
            self.codes = tuple(
                sorted(order_book_id_to_baostock(value) for value in self._instruments)
            )
        if self.start_date is None:
            self.start_date = self._calendar[0].date()
        if self.end_date is None:
            self.end_date = self._calendar[-1].date()
        self._bars, self._bar_metadata = self._load_bars()
        self._corporate_actions = self.store.scan(
            "corporate_actions",
            as_of=self.as_of,
            contained_in={"code": self.codes},
            ranges={"ex_date": (self.start_date, self.end_date)},
        ).to_pylist()

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
        metadata = self._bar_metadata.get(
            order_book_id,
            np.empty(0, dtype=METADATA_DTYPE),
        )
        return [
            not _metadata_value(metadata, _date_int(_as_date(value)), "trade_status")
            for value in dates
        ]

    def is_st_stock(self, order_book_id: str, dates: Sequence[Any]) -> list[bool]:
        metadata = self._bar_metadata.get(
            order_book_id,
            np.empty(0, dtype=METADATA_DTYPE),
        )
        return [
            _metadata_value(metadata, _date_int(_as_date(value)), "is_st")
            for value in dates
        ]

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
        ranges = (
            {"calendar_date": (self.start_date, self.end_date)}
            if self.start_date is not None or self.end_date is not None
            else None
        )
        rows = self.store.scan(
            "trade_calendar",
            as_of=self.as_of,
            columns=("calendar_date", "is_trading_day"),
            ranges=ranges,
        ).to_pylist()
        dates = [row["calendar_date"] for row in rows if row["is_trading_day"]]
        calendar = pd.DatetimeIndex(sorted(dates))
        if calendar.empty:
            raise ValueError("bounded normalized trade calendar is empty")
        return calendar

    def _load_instruments(self) -> dict[str, Instrument]:
        result = {}
        contained_in = {"code": self.codes} if self.codes else None
        for row in self.store.scan(
            "instruments",
            as_of=self.as_of,
            contained_in=contained_in,
        ).to_pylist():
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
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
        assert self.start_date is not None and self.end_date is not None
        series = self.store.daily_bar_series(
            codes=self.codes,
            start_date=self.start_date,
            end_date=self.end_date,
            as_of=self.as_of,
            adjustment_flags=("3",),
            columns=(
                "trade_date",
                "code",
                "open",
                "high",
                "low",
                "close",
                "previous_close",
                "volume",
                "amount",
                "adjustment_flag",
                "trade_status",
                "is_st",
            ),
        )
        arrays: dict[str, np.ndarray] = {}
        metadata: dict[str, np.ndarray] = {}
        for (code, _), table in series.items():
            order_book_id = baostock_to_order_book_id(code)
            instrument = self._instruments.get(order_book_id)
            if instrument is None:
                continue
            dates = np.fromiter(
                (_date_int(value) for value in table["trade_date"].to_pylist()),
                dtype=np.uint64,
                count=table.num_rows,
            )
            previous_close = _float_column(table, "previous_close")
            is_st = table["is_st"].combine_chunks().to_numpy(zero_copy_only=False)
            ratio = np.where(is_st, 0.05, _instrument_limit_ratio(instrument))
            limit_up = _round_half_up(previous_close * (1 + ratio), decimals=2)
            limit_down = _round_half_up(previous_close * (1 - ratio), decimals=2)

            bars = np.empty(table.num_rows, dtype=BAR_DTYPE)
            bars["datetime"] = dates
            for field in ("open", "high", "low", "close"):
                bars[field] = _float_column(table, field)
            bars["prev_close"] = previous_close
            bars["limit_up"] = limit_up
            bars["limit_down"] = limit_down
            bars["volume"] = _float_column(table, "volume")
            bars["total_turnover"] = _float_column(table, "amount")
            arrays[order_book_id] = bars

            flags = np.empty(table.num_rows, dtype=METADATA_DTYPE)
            flags["datetime"] = dates
            flags["trade_status"] = table["trade_status"].combine_chunks().to_numpy(
                zero_copy_only=False
            )
            flags["is_st"] = is_st
            metadata[order_book_id] = flags
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


def _instrument_limit_ratio(instrument: Instrument) -> float:
    if instrument.board_type in {"KSH", "GEM"}:
        return 0.20
    if instrument.board_type == "BJS":
        return 0.30
    return 0.10


def _limit_price(previous_close: Decimal, ratio: Decimal) -> float:
    price = previous_close * (Decimal(1) + ratio)
    return float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _float_or_nan(value: Decimal | None) -> float:
    return float(value) if value is not None else np.nan


def _float_column(table: Any, name: str) -> np.ndarray:
    return table[name].cast("double").combine_chunks().to_numpy(zero_copy_only=False)


def _round_half_up(values: np.ndarray, *, decimals: int) -> np.ndarray:
    scale = 10**decimals
    return np.floor(values * scale + 0.5) / scale


def _metadata_value(metadata: np.ndarray, target: int, field: str) -> bool:
    index = int(np.searchsorted(metadata["datetime"], target, side="left"))
    if index >= len(metadata) or metadata[index]["datetime"] != target:
        return False
    return bool(metadata[index][field])


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
