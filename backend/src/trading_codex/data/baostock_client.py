from collections.abc import Callable
from datetime import UTC, date, datetime
from types import ModuleType
from typing import Any

from trading_codex.data.models import ProviderBatch, ProviderError

DAILY_FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "turn",
    "tradestatus",
    "pctChg",
    "isST",
)
FIVE_MINUTE_FIELDS = (
    "date",
    "time",
    "code",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adjustflag",
)


class BaoStockClient:
    def __init__(
        self,
        *,
        module: ModuleType | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if module is None:
            raise ProviderError(
                "legacy BaoStock adapter is disabled; use trading-codex-baostock"
            )
        self._module = module
        self._clock = clock or (lambda: datetime.now(UTC))
        self._logged_in = False

    def __enter__(self) -> "BaoStockClient":
        try:
            response = self._module.login()
        except Exception as exc:
            raise ProviderError("BaoStock login failed") from exc
        self._ensure_success(response, operation="login")
        self._logged_in = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._logged_in:
            return
        try:
            response = self._module.logout()
            self._ensure_success(response, operation="logout")
        except Exception:
            if exc is None:
                raise
        finally:
            self._logged_in = False

    def instruments(self, *, code: str = "") -> ProviderBatch:
        return self._collect(
            "instruments",
            {"code": code},
            self._module.query_stock_basic(code=code),
        )

    def trade_calendar(self, *, start_date: date, end_date: date) -> ProviderBatch:
        query = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
        return self._collect(
            "trade_calendar",
            query,
            self._module.query_trade_dates(**query),
        )

    def historical_universe(self, *, day: date) -> ProviderBatch:
        query = {"day": day.isoformat()}
        return self._collect(
            "historical_universe",
            query,
            self._module.query_all_stock(**query),
        )

    def daily_bars(
        self,
        *,
        code: str,
        start_date: date,
        end_date: date,
        adjustment_flag: str = "3",
    ) -> ProviderBatch:
        if adjustment_flag not in {"1", "2", "3"}:
            raise ValueError("adjustment_flag must be one of 1, 2, or 3")
        query = {
            "code": code,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "frequency": "d",
            "adjustflag": adjustment_flag,
        }
        result = self._module.query_history_k_data_plus(
            code,
            ",".join(DAILY_FIELDS),
            start_date=query["start_date"],
            end_date=query["end_date"],
            frequency=query["frequency"],
            adjustflag=query["adjustflag"],
        )
        return self._collect("daily_bars", query, result)

    def adjustment_factors(
        self, *, code: str, start_date: date, end_date: date
    ) -> ProviderBatch:
        query = {
            "code": code,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        return self._collect(
            "adjustment_factors",
            query,
            self._module.query_adjust_factor(**query),
        )

    def five_minute_bars(
        self, *, code: str, start_date: date, end_date: date
    ) -> ProviderBatch:
        query = {
            "code": code,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "frequency": "5",
            "adjustflag": "3",
        }
        result = self._module.query_history_k_data_plus(
            code,
            ",".join(FIVE_MINUTE_FIELDS),
            start_date=query["start_date"],
            end_date=query["end_date"],
            frequency=query["frequency"],
            adjustflag=query["adjustflag"],
        )
        return self._collect("five_minute_bars", query, result)

    def _collect(self, operation: str, query: dict[str, str], result: Any) -> ProviderBatch:
        self._ensure_success(result, operation=operation)
        fields = tuple(result.fields)
        rows: list[dict[str, str]] = []
        while result.next():
            values = result.get_row_data()
            if len(values) != len(fields):
                raise ProviderError(f"BaoStock returned a malformed {operation} row")
            rows.append(dict(zip(fields, values, strict=True)))
        self._ensure_success(result, operation=operation)
        return ProviderBatch(
            source="baostock",
            operation=operation,
            query=query,
            fields=fields,
            rows=tuple(rows),
            received_at=self._clock(),
        )

    @staticmethod
    def _ensure_success(result: Any, *, operation: str) -> None:
        if result is None:
            raise ProviderError(f"BaoStock {operation} returned no result")
        error_code = getattr(result, "error_code", None)
        if error_code != "0":
            error_message = getattr(result, "error_msg", "unknown error")
            raise ProviderError(f"BaoStock {operation} failed ({error_code}): {error_message}")
