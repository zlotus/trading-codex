from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from trading_codex.baostock_download.constants import BLACKLIST_ERROR_CODE
from trading_codex.baostock_download.errors import (
    BaoStockDownloadError,
    ProviderBlacklisted,
    ProviderFailure,
)
from trading_codex.data.models import ProviderBatch


class ProviderRequest(Protocol):
    operation: str
    query: dict[str, str]
    expected_fields: tuple[str, ...]

    @property
    def raw_query(self) -> dict[str, str]: ...


class BaoStockProvider:
    """Thin endpoint adapter shared by the current and legacy download paths."""

    def __init__(
        self,
        module: Any,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.module = module
        self.clock = clock or (lambda: datetime.now(UTC))
        self.logged_in = False

    def login(self) -> None:
        try:
            result = self.module.login()
        except BaoStockDownloadError:
            raise
        except Exception as exc:
            raise ProviderFailure("BaoStock login raised an exception") from exc
        self._ensure_success(result, operation="login")
        self.logged_in = True

    def logout(self) -> None:
        if not self.logged_in:
            return
        try:
            result = self.module.logout()
        except BaoStockDownloadError:
            raise
        except Exception as exc:
            raise ProviderFailure("BaoStock logout raised an exception") from exc
        finally:
            self.logged_in = False
        self._ensure_success(result, operation="logout")

    def fetch(self, item: ProviderRequest) -> ProviderBatch:
        if not self.logged_in:
            raise ProviderFailure("BaoStock query attempted before login")
        query = item.query
        try:
            result = self._query(item, query)
        except BaoStockDownloadError:
            raise
        except Exception as exc:
            raise ProviderFailure(f"BaoStock {item.operation} raised an exception") from exc
        return self._collect(item, result)

    def close_local_socket(self) -> None:
        custom = getattr(self.module, "close_local_socket", None)
        if callable(custom):
            custom()
            self.logged_in = False
            return
        try:
            import baostock.common.context as context

            sock = getattr(context, "default_socket", None)
            if sock is not None:
                sock.close()
                setattr(context, "default_socket", None)
        finally:
            self.logged_in = False

    def _query(self, item: ProviderRequest, query: dict[str, str]) -> Any:
        method_name = getattr(item, "provider_method", None)
        if not isinstance(method_name, str):
            method_name = getattr(item, "endpoint", None)
        if not isinstance(method_name, str):
            raise ProviderFailure(f"missing provider method for {item.operation}")
        method = getattr(self.module, method_name)
        if item.operation == "instruments":
            return method(code=query.get("code", ""))
        if item.operation == "trade_calendar":
            return method(start_date=query["start_date"], end_date=query["end_date"])
        if item.operation == "historical_universe":
            return method(day=query["day"])
        if item.operation in {"daily_bars", "five_minute_bars"}:
            return method(
                query["code"],
                ",".join(item.expected_fields),
                start_date=query["start_date"],
                end_date=query["end_date"],
                frequency=query["frequency"],
                adjustflag=query["adjustflag"],
            )
        if item.operation == "adjustment_factors":
            return method(
                code=query["code"],
                start_date=query["start_date"],
                end_date=query["end_date"],
            )
        if item.operation in {
            "daily_history_astock",
            "daily_adjust_factors",
            "hs300_stocks",
            "zz500_stocks",
        }:
            return method(date=query["date"])
        if item.operation == "dividends":
            return method(
                code=query["code"],
                year=query["year"],
                yearType=query["yearType"],
            )
        raise ProviderFailure(f"unsupported provider operation: {item.operation}")

    def _collect(self, item: ProviderRequest, result: Any) -> ProviderBatch:
        self._ensure_success(result, operation=item.operation)
        fields = tuple(getattr(result, "fields", ()))
        rows = []
        while result.next():
            values = result.get_row_data()
            if len(values) != len(fields):
                raise ProviderFailure(f"BaoStock returned a malformed {item.operation} row")
            rows.append(dict(zip(fields, values, strict=True)))
        self._ensure_success(result, operation=item.operation)
        received_at = self.clock()
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ProviderFailure("provider clock returned a naive timestamp")
        return ProviderBatch(
            source="baostock",
            operation=item.operation,
            query=item.raw_query,
            fields=fields,
            rows=tuple(rows),
            received_at=received_at,
        )

    @staticmethod
    def _ensure_success(result: Any, *, operation: str) -> None:
        if result is None:
            raise ProviderFailure(f"BaoStock {operation} returned no result")
        code = getattr(result, "error_code", None)
        message = getattr(result, "error_msg", "unknown error")
        if code == BLACKLIST_ERROR_CODE:
            raise ProviderBlacklisted(
                f"BaoStock {operation} returned {BLACKLIST_ERROR_CODE}: {message}"
            )
        if code != "0":
            raise ProviderFailure(f"BaoStock {operation} failed ({code}): {message}")
