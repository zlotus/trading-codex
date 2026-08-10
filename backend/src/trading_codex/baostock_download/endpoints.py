import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from trading_codex.baostock_download.errors import ManifestError

INSTRUMENT_FIELDS = ("code", "code_name", "ipoDate", "outDate", "type", "status")
TRADE_CALENDAR_FIELDS = ("calendar_date", "is_trading_day")
HISTORICAL_UNIVERSE_FIELDS = ("code", "tradeStatus", "code_name")
DAILY_BAR_FIELDS = (
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
BULK_DAILY_BAR_FIELDS = (
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
    "peTTM",
    "pbMRQ",
    "psTTM",
    "pcfNcfTTM",
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
ADJUSTMENT_FACTOR_FIELDS = (
    "code",
    "dividOperateDate",
    "foreAdjustFactor",
    "backAdjustFactor",
    "adjustFactor",
)
INDEX_MEMBERSHIP_FIELDS = ("updateDate", "code", "code_name")
DIVIDEND_FIELDS = (
    "code",
    "dividPreNoticeDate",
    "dividAgmPumDate",
    "dividPlanAnnounceDate",
    "dividPlanDate",
    "dividRegistDate",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
    "dividCashPsBeforeTax",
    "dividCashPsAfterTax",
    "dividStocksPs",
    "dividCashStock",
    "dividReserveToStockPs",
)

_CODE = re.compile(r"^(?:sh|sz)\.\d{6}$")
_DATE_FIELDS = {"date", "day", "start_date", "end_date"}


@dataclass(frozen=True)
class EndpointContract:
    operation: str
    provider_method: str
    required_query: tuple[str, ...]
    optional_query: tuple[str, ...]
    expected_fields: tuple[str, ...]
    normalized_operation: str
    paginated: bool

    def validate_query(self, raw_query: Any) -> dict[str, str]:
        if not isinstance(raw_query, dict):
            raise ManifestError(f"{self.operation} query must be an object")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_query.items()
        ):
            raise ManifestError(f"{self.operation} query keys and values must be strings")
        query = dict(sorted(raw_query.items()))
        allowed = set(self.required_query) | set(self.optional_query)
        missing = [field for field in self.required_query if not query.get(field)]
        extra = sorted(set(query) - allowed)
        if missing:
            raise ManifestError(
                f"{self.operation} query is missing required fields: {', '.join(missing)}"
            )
        if extra:
            raise ManifestError(
                f"{self.operation} query has unsupported fields: {', '.join(extra)}"
            )
        for field in _DATE_FIELDS & set(query):
            try:
                date.fromisoformat(query[field])
            except ValueError as exc:
                raise ManifestError(
                    f"{self.operation} query has invalid {field}: {query[field]!r}"
                ) from exc
        if "code" in query and query["code"] and not _CODE.fullmatch(query["code"]):
            raise ManifestError(f"{self.operation} query has invalid BaoStock code")
        if "adjustflag" in query and query["adjustflag"] not in {"1", "2", "3"}:
            raise ManifestError(f"{self.operation} adjustflag must be 1, 2, or 3")
        if "frequency" in query:
            required = "5" if self.operation == "five_minute_bars" else "d"
            if query["frequency"] != required:
                raise ManifestError(f"{self.operation} frequency must be {required}")
        if "year" in query and (len(query["year"]) != 4 or not query["year"].isdigit()):
            raise ManifestError(f"{self.operation} year must contain four digits")
        if "yearType" in query and query["yearType"] not in {"report", "operate"}:
            raise ManifestError(f"{self.operation} yearType must be report or operate")
        self._validate_range(query)
        return query

    def _validate_range(self, query: dict[str, str]) -> None:
        if "start_date" not in query or "end_date" not in query:
            return
        start = date.fromisoformat(query["start_date"])
        end = date.fromisoformat(query["end_date"])
        if end < start:
            raise ManifestError(f"{self.operation} end_date precedes start_date")
        days = (end - start).days + 1
        if self.operation == "five_minute_bars" and days > 31:
            raise ManifestError(
                "five_minute_bars chunk exceeds the conservative 20-trading-day bound"
            )


ENDPOINTS = {
    contract.operation: contract
    for contract in (
        EndpointContract(
            "instruments",
            "query_stock_basic",
            (),
            ("code",),
            INSTRUMENT_FIELDS,
            "instruments",
            True,
        ),
        EndpointContract(
            "trade_calendar",
            "query_trade_dates",
            ("start_date", "end_date"),
            (),
            TRADE_CALENDAR_FIELDS,
            "trade_calendar",
            True,
        ),
        EndpointContract(
            "historical_universe",
            "query_all_stock",
            ("day",),
            (),
            HISTORICAL_UNIVERSE_FIELDS,
            "historical_universe",
            True,
        ),
        EndpointContract(
            "daily_bars",
            "query_history_k_data_plus",
            ("code", "start_date", "end_date", "frequency", "adjustflag"),
            (),
            DAILY_BAR_FIELDS,
            "daily_bars",
            True,
        ),
        EndpointContract(
            "adjustment_factors",
            "query_adjust_factor",
            ("code", "start_date", "end_date"),
            (),
            ADJUSTMENT_FACTOR_FIELDS,
            "adjustment_factors",
            True,
        ),
        EndpointContract(
            "five_minute_bars",
            "query_history_k_data_plus",
            ("code", "start_date", "end_date", "frequency", "adjustflag"),
            (),
            FIVE_MINUTE_FIELDS,
            "five_minute_bars",
            True,
        ),
        EndpointContract(
            "daily_history_astock",
            "query_daily_history_k_AStock",
            ("date",),
            (),
            BULK_DAILY_BAR_FIELDS,
            "daily_bars",
            False,
        ),
        EndpointContract(
            "daily_adjust_factors",
            "query_daily_adjust_factor",
            ("date",),
            (),
            ADJUSTMENT_FACTOR_FIELDS,
            "adjustment_factors",
            False,
        ),
        EndpointContract(
            "hs300_stocks",
            "query_hs300_stocks",
            ("date",),
            (),
            INDEX_MEMBERSHIP_FIELDS,
            "index_memberships",
            True,
        ),
        EndpointContract(
            "zz500_stocks",
            "query_zz500_stocks",
            ("date",),
            (),
            INDEX_MEMBERSHIP_FIELDS,
            "index_memberships",
            True,
        ),
        EndpointContract(
            "dividends",
            "query_dividend_data",
            ("code", "year", "yearType"),
            (),
            DIVIDEND_FIELDS,
            "corporate_actions",
            True,
        ),
    )
}


def endpoint(operation: str) -> EndpointContract:
    try:
        return ENDPOINTS[operation]
    except KeyError as exc:
        raise ManifestError(f"unsupported BaoStock operation: {operation}") from exc
