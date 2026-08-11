from datetime import UTC, date, datetime

import pytest

from trading_codex.data.baostock_client import BaoStockClient
from trading_codex.data.models import ProviderError


class Result:
    def __init__(
        self,
        *,
        fields: list[str] | None = None,
        rows: list[list[str]] | None = None,
        error_code: str = "0",
        error_msg: str = "success",
    ) -> None:
        self.fields = fields or []
        self.rows = rows or []
        self.error_code = error_code
        self.error_msg = error_msg
        self.index = 0

    def next(self) -> bool:
        return self.index < len(self.rows)

    def get_row_data(self) -> list[str]:
        row = self.rows[self.index]
        self.index += 1
        return row


class Module:
    def __init__(self) -> None:
        self.last_adjustment_flag: str | None = None

    def login(self) -> Result:
        return Result()

    def logout(self) -> Result:
        return Result()

    def query_trade_dates(self, *, start_date: str, end_date: str) -> Result:
        assert start_date == "2024-01-01"
        assert end_date == "2024-01-02"
        return Result(
            fields=["calendar_date", "is_trading_day"],
            rows=[["2024-01-01", "0"], ["2024-01-02", "1"]],
        )

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        *,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
    ) -> Result:
        assert code == "sh.600000"
        assert fields
        assert start_date == "2024-01-02"
        assert end_date == "2024-01-03"
        assert frequency == "d"
        self.last_adjustment_flag = adjustflag
        return Result(fields=["date", "adjustflag"])


def test_legacy_client_has_no_implicit_production_provider() -> None:
    with pytest.raises(ProviderError, match="legacy BaoStock adapter is disabled"):
        BaoStockClient()


def test_client_collects_rows_without_pandas_result_helper() -> None:
    received_at = datetime(2024, 1, 3, tzinfo=UTC)
    client = BaoStockClient(module=Module(), clock=lambda: received_at)  # type: ignore[arg-type]

    with client:
        batch = client.trade_calendar(
            start_date=date(2024, 1, 1), end_date=date(2024, 1, 2)
        )

    assert batch.received_at == received_at
    assert batch.fields == ("calendar_date", "is_trading_day")
    assert batch.rows[1] == {"calendar_date": "2024-01-02", "is_trading_day": "1"}


def test_daily_bars_passes_explicit_adjustment_flag() -> None:
    module = Module()
    client = BaoStockClient(module=module)  # type: ignore[arg-type]

    batch = client.daily_bars(
        code="sh.600000",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        adjustment_flag="2",
    )

    assert module.last_adjustment_flag == "2"
    assert batch.query["adjustflag"] == "2"


def test_daily_bars_rejects_unknown_adjustment_flag() -> None:
    with pytest.raises(ValueError, match="adjustment_flag"):
        BaoStockClient(module=Module()).daily_bars(  # type: ignore[arg-type]
            code="sh.600000",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 3),
            adjustment_flag="9",
        )


class FailedLoginModule(Module):
    def login(self) -> Result:
        return Result(error_code="10002007", error_msg="network error")


def test_client_wraps_provider_failure() -> None:
    with pytest.raises(ProviderError, match="login failed"):
        with BaoStockClient(module=FailedLoginModule()):  # type: ignore[arg-type]
            pass
