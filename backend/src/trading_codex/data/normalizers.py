import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Any

from trading_codex.data.models import DataValidationError, ProviderBatch, RawArtifact
from trading_codex.data.schemas import PRICE
from trading_codex.data.time import SHANGHAI, parse_baostock_bar_time, parse_date, shanghai_at

NormalizedRow = dict[str, Any]
Normalizer = Callable[[ProviderBatch, RawArtifact], list[NormalizedRow]]
PRICE_QUANTUM = Decimal(1).scaleb(-PRICE.scale)
PRICE_CONTEXT = Context(prec=PRICE.precision, rounding=ROUND_HALF_EVEN)


def _required(row: dict[str, str], field: str) -> str:
    try:
        value = row[field]
    except KeyError as exc:
        raise DataValidationError(f"missing provider field: {field}") from exc
    if value == "":
        raise DataValidationError(f"blank required provider field: {field}")
    return value


def _decimal(value: str, *, field: str, optional: bool = False) -> Decimal | None:
    if value == "" and optional:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise DataValidationError(f"invalid decimal {field}: {value!r}") from exc


def _price(value: str, *, field: str, optional: bool = False) -> Decimal | None:
    parsed = _decimal(value, field=field, optional=optional)
    if parsed is None:
        return None
    try:
        with localcontext(PRICE_CONTEXT):
            return parsed.quantize(PRICE_QUANTUM)
    except InvalidOperation as exc:
        raise DataValidationError(
            f"price {field} exceeds normalized precision: {value!r}"
        ) from exc


def _integer(value: str, *, field: str) -> int:
    parsed = _decimal(value, field=field)
    assert parsed is not None
    if parsed != parsed.to_integral_value():
        raise DataValidationError(f"non-integral {field}: {value!r}")
    return int(parsed)


def _flag(value: str, *, field: str) -> bool:
    if value not in {"0", "1"}:
        raise DataValidationError(f"invalid flag {field}: {value!r}")
    return value == "1"


def _provenance(
    batch: ProviderBatch,
    artifact: RawArtifact,
    *,
    available_at: datetime,
) -> NormalizedRow:
    return {
        "available_at": available_at,
        "source": batch.source,
        "source_received_at": artifact.received_at,
        "source_payload_sha256": artifact.content_sha256,
        "raw_artifact": artifact.relative_path,
    }


def normalize_instruments(batch: ProviderBatch, artifact: RawArtifact) -> list[NormalizedRow]:
    normalized = []
    for row in batch.rows:
        code = _required(row, "code")
        _validate_query_code(batch, code)
        normalized.append(
            {
                "code": code,
                "name": _required(row, "code_name"),
                "ipo_date": parse_date(_required(row, "ipoDate"), field="ipoDate"),
                "out_date": parse_date(row.get("outDate", ""), field="outDate", optional=True),
                "security_type": _required(row, "type"),
                "status": _required(row, "status"),
                **_provenance(batch, artifact, available_at=artifact.received_at),
            }
        )
    return normalized


def normalize_trade_calendar(batch: ProviderBatch, artifact: RawArtifact) -> list[NormalizedRow]:
    normalized = []
    for row in batch.rows:
        calendar_date = parse_date(
            _required(row, "calendar_date"), field="calendar_date"
        )
        assert calendar_date is not None
        _validate_query_date_range(batch, calendar_date)
        normalized.append(
            {
                "calendar_date": calendar_date,
                "is_trading_day": _flag(
                    _required(row, "is_trading_day"), field="is_trading_day"
                ),
                **_provenance(
                    batch,
                    artifact,
                    available_at=shanghai_at(calendar_date, time.min),
                ),
            }
        )
    return normalized


def normalize_historical_universe(
    batch: ProviderBatch, artifact: RawArtifact
) -> list[NormalizedRow]:
    snapshot_date = parse_date(_required_query(batch, "day"), field="day")
    assert snapshot_date is not None
    normalized = []
    for row in batch.rows:
        normalized.append(
            {
                "snapshot_date": snapshot_date,
                "code": _required(row, "code"),
                "name": _required(row, "code_name"),
                "trade_status": _flag(
                    _required(row, "tradeStatus"), field="tradeStatus"
                ),
                **_provenance(
                    batch,
                    artifact,
                    available_at=shanghai_at(snapshot_date, time(9, 0)),
                ),
            }
        )
    return normalized


def normalize_index_memberships(
    batch: ProviderBatch, artifact: RawArtifact
) -> list[NormalizedRow]:
    snapshot_date = parse_date(_required_query(batch, "date"), field="date")
    assert snapshot_date is not None
    index_codes = {
        "hs300_stocks": "sh.000300",
        "zz500_stocks": "sh.000905",
    }
    try:
        index_code = index_codes[batch.operation]
    except KeyError as exc:
        raise DataValidationError(
            f"unsupported index membership operation: {batch.operation}"
        ) from exc
    normalized = []
    for row in batch.rows:
        update_date = parse_date(_required(row, "updateDate"), field="updateDate")
        assert update_date is not None
        if update_date > snapshot_date:
            raise DataValidationError("index membership updateDate exceeds snapshot date")
        normalized.append(
            {
                "snapshot_date": snapshot_date,
                "index_code": index_code,
                "member_code": _required(row, "code"),
                "member_name": _required(row, "code_name"),
                **_provenance(
                    batch,
                    artifact,
                    available_at=shanghai_at(snapshot_date, time(9, 0)),
                ),
            }
        )
    return normalized


def normalize_daily_bars(batch: ProviderBatch, artifact: RawArtifact) -> list[NormalizedRow]:
    normalized = []
    for row in batch.rows:
        trade_date = parse_date(_required(row, "date"), field="date")
        assert trade_date is not None
        _validate_trade_date(batch, trade_date)
        code = _required(row, "code")
        _validate_query_code(batch, code)
        trade_status = _flag(_required(row, "tradestatus"), field="tradestatus")
        prices = {
            "open": _price(row.get("open", ""), field="open", optional=True),
            "high": _price(row.get("high", ""), field="high", optional=True),
            "low": _price(row.get("low", ""), field="low", optional=True),
            "close": _price(row.get("close", ""), field="close", optional=True),
            "previous_close": _price(
                row.get("preclose", ""), field="preclose", optional=True
            ),
        }
        volume = _integer(_required(row, "volume"), field="volume")
        amount = _decimal(row.get("amount", ""), field="amount", optional=True)
        _validate_bar(prices, volume=volume, amount=amount, trading=trade_status)
        normalized.append(
            {
                "trade_date": trade_date,
                "code": code,
                **prices,
                "volume": volume,
                "amount": amount,
                "adjustment_flag": _adjustment_flag(row),
                "turnover": _decimal(row.get("turn", ""), field="turn", optional=True),
                "trade_status": trade_status,
                "pct_change": _decimal(
                    row.get("pctChg", ""), field="pctChg", optional=True
                ),
                "is_st": _flag(_required(row, "isST"), field="isST"),
                **_provenance(
                    batch,
                    artifact,
                    available_at=shanghai_at(trade_date, time(15, 0)),
                ),
            }
        )
    return normalized


def normalize_adjustment_factors(
    batch: ProviderBatch, artifact: RawArtifact
) -> list[NormalizedRow]:
    normalized = []
    for row in batch.rows:
        effective_date = parse_date(
            _required(row, "dividOperateDate"), field="dividOperateDate"
        )
        assert effective_date is not None
        code = _required(row, "code")
        _validate_query_code(batch, code)
        if batch.operation == "daily_adjust_factors":
            _validate_exact_query_date(batch, effective_date)
        else:
            _validate_query_date_range(batch, effective_date)
        normalized.append(
            {
                "code": code,
                "effective_date": effective_date,
                "forward_factor": _decimal(
                    _required(row, "foreAdjustFactor"), field="foreAdjustFactor"
                ),
                "backward_factor": _decimal(
                    _required(row, "backAdjustFactor"), field="backAdjustFactor"
                ),
                "adjustment_factor": _decimal(
                    _required(row, "adjustFactor"), field="adjustFactor"
                ),
                **_provenance(
                    batch,
                    artifact,
                    available_at=shanghai_at(effective_date, time(9, 25)),
                ),
            }
        )
    return normalized


def normalize_dividends(batch: ProviderBatch, artifact: RawArtifact) -> list[NormalizedRow]:
    normalized = []
    for row in batch.rows:
        announcement_date = parse_date(
            _required(row, "dividPlanAnnounceDate"), field="dividPlanAnnounceDate"
        )
        ex_date = parse_date(
            _required(row, "dividOperateDate"), field="dividOperateDate"
        )
        assert announcement_date is not None and ex_date is not None
        if announcement_date > ex_date:
            raise DataValidationError("dividend announcement date exceeds ex date")
        code = _required(row, "code")
        _validate_query_code(batch, code)
        cash = _decimal_or_zero(row.get("dividCashPsBeforeTax", ""), field="dividCashPsBeforeTax")
        stock = _decimal_or_zero(row.get("dividStocksPs", ""), field="dividStocksPs")
        capitalization = _decimal_or_zero(
            row.get("dividReserveToStockPs", ""), field="dividReserveToStockPs"
        )
        identity = json.dumps(
            {
                "code": code,
                "announcement_date": announcement_date.isoformat(),
                "ex_date": ex_date.isoformat(),
                "cash": str(cash),
                "stock": str(stock),
                "capitalization": str(capitalization),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        normalized.append(
            {
                "action_id": hashlib.sha256(identity).hexdigest(),
                "code": code,
                "announcement_date": announcement_date,
                "record_date": parse_date(
                    row.get("dividRegistDate", ""),
                    field="dividRegistDate",
                    optional=True,
                ),
                "ex_date": ex_date,
                "pay_date": parse_date(
                    row.get("dividPayDate", ""), field="dividPayDate", optional=True
                ),
                "cash_before_tax_per_share": cash,
                "stock_dividend_ratio": stock,
                "capitalization_ratio": capitalization,
                **_provenance(
                    batch,
                    artifact,
                    # BaoStock exposes only an announcement date, not a publication time.
                    available_at=shanghai_at(
                        announcement_date + timedelta(days=1), time.min
                    ),
                ),
            }
        )
    return normalized


def _decimal_or_zero(value: str, *, field: str) -> Decimal:
    parsed = _decimal(value, field=field, optional=True)
    return parsed if parsed is not None else Decimal(0)


def normalize_five_minute_bars(
    batch: ProviderBatch, artifact: RawArtifact
) -> list[NormalizedRow]:
    normalized = []
    for row in batch.rows:
        trade_date = parse_date(_required(row, "date"), field="date")
        assert trade_date is not None
        timestamp = parse_baostock_bar_time(_required(row, "time"))
        _validate_trade_date(batch, trade_date)
        code = _required(row, "code")
        _validate_query_code(batch, code)
        if timestamp.astimezone(SHANGHAI).date() != trade_date:
            raise DataValidationError("five-minute bar timestamp does not match its trade date")
        prices = {
            "open": _price(_required(row, "open"), field="open"),
            "high": _price(_required(row, "high"), field="high"),
            "low": _price(_required(row, "low"), field="low"),
            "close": _price(_required(row, "close"), field="close"),
        }
        volume = _integer(_required(row, "volume"), field="volume")
        amount = _decimal(_required(row, "amount"), field="amount")
        _validate_bar(prices, volume=volume, amount=amount, trading=True)
        normalized.append(
            {
                "timestamp": timestamp,
                "trade_date": trade_date,
                "code": code,
                **prices,
                "volume": volume,
                "amount": amount,
                "adjustment_flag": _adjustment_flag(row),
                **_provenance(batch, artifact, available_at=timestamp),
            }
        )
    return normalized


def _required_query(batch: ProviderBatch, field: str) -> str:
    try:
        value = batch.query[field]
    except KeyError as exc:
        raise DataValidationError(f"missing provider query field: {field}") from exc
    if not value:
        raise DataValidationError(f"blank provider query field: {field}")
    return value


def _adjustment_flag(row: dict[str, str]) -> str:
    value = _required(row, "adjustflag")
    if value not in {"1", "2", "3"}:
        raise DataValidationError(f"invalid adjustment flag: {value!r}")
    return value


def _validate_trade_date(batch: ProviderBatch, trade_date: date) -> None:
    if batch.operation == "daily_history_astock":
        _validate_exact_query_date(batch, trade_date)
        return
    if batch.operation not in {"daily_bars", "five_minute_bars"}:
        return
    _validate_query_date_range(batch, trade_date)


def _validate_query_code(batch: ProviderBatch, code: str) -> None:
    expected = batch.query.get("code")
    if expected and code != expected:
        raise DataValidationError(
            f"provider row code {code!r} differs from exact query code {expected!r}"
        )


def _validate_exact_query_date(batch: ProviderBatch, actual: date) -> None:
    expected = parse_date(_required_query(batch, "date"), field="date")
    if actual != expected:
        raise DataValidationError(
            f"provider row date {actual.isoformat()} differs from exact query date"
        )


def _validate_query_date_range(batch: ProviderBatch, actual: date) -> None:
    start = parse_date(_required_query(batch, "start_date"), field="start_date")
    end = parse_date(_required_query(batch, "end_date"), field="end_date")
    assert start is not None and end is not None
    if not start <= actual <= end:
        raise DataValidationError("provider row date is outside its exact query range")


def _validate_bar(
    prices: dict[str, Decimal | None],
    *,
    volume: int,
    amount: Decimal | None,
    trading: bool,
) -> None:
    if volume < 0 or (amount is not None and amount < 0):
        raise DataValidationError("bar volume and amount must be non-negative")
    ohlc = [prices[name] for name in ("open", "high", "low", "close")]
    if trading and (any(value is None for value in ohlc) or any(value <= 0 for value in ohlc)):
        raise DataValidationError("trading bar must have positive OHLC values")
    if any(value is None for value in ohlc):
        return
    open_price, high, low, close = ohlc
    assert open_price is not None and high is not None and low is not None and close is not None
    if high < max(open_price, low, close) or low > min(open_price, high, close):
        raise DataValidationError("bar OHLC values are internally inconsistent")


NORMALIZERS: dict[str, tuple[str, Normalizer]] = {
    "instruments": ("instruments", normalize_instruments),
    "trade_calendar": ("trade_calendar", normalize_trade_calendar),
    "historical_universe": ("historical_universe", normalize_historical_universe),
    "daily_bars": ("daily_bars", normalize_daily_bars),
    "daily_history_astock": ("daily_bars", normalize_daily_bars),
    "adjustment_factors": ("adjustment_factors", normalize_adjustment_factors),
    "daily_adjust_factors": ("adjustment_factors", normalize_adjustment_factors),
    "five_minute_bars": ("five_minute_bars", normalize_five_minute_bars),
    "hs300_stocks": ("index_memberships", normalize_index_memberships),
    "zz500_stocks": ("index_memberships", normalize_index_memberships),
    "dividends": ("corporate_actions", normalize_dividends),
}


def normalize_batch(
    batch: ProviderBatch, artifact: RawArtifact
) -> tuple[str, list[NormalizedRow]]:
    try:
        dataset, normalizer = NORMALIZERS[batch.operation]
    except KeyError as exc:
        raise DataValidationError(f"unsupported provider operation: {batch.operation}") from exc
    return dataset, normalizer(batch, artifact)
