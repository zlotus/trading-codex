from collections.abc import Callable
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_codex.data.models import DataValidationError, ProviderBatch, RawArtifact
from trading_codex.data.time import parse_baostock_bar_time, parse_date, shanghai_at

NormalizedRow = dict[str, Any]
Normalizer = Callable[[ProviderBatch, RawArtifact], list[NormalizedRow]]


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
        normalized.append(
            {
                "code": _required(row, "code"),
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


def normalize_daily_bars(batch: ProviderBatch, artifact: RawArtifact) -> list[NormalizedRow]:
    normalized = []
    for row in batch.rows:
        trade_date = parse_date(_required(row, "date"), field="date")
        assert trade_date is not None
        trade_status = _flag(_required(row, "tradestatus"), field="tradestatus")
        prices = {
            "open": _decimal(row.get("open", ""), field="open", optional=True),
            "high": _decimal(row.get("high", ""), field="high", optional=True),
            "low": _decimal(row.get("low", ""), field="low", optional=True),
            "close": _decimal(row.get("close", ""), field="close", optional=True),
            "previous_close": _decimal(
                row.get("preclose", ""), field="preclose", optional=True
            ),
        }
        volume = _integer(_required(row, "volume"), field="volume")
        amount = _decimal(row.get("amount", ""), field="amount", optional=True)
        _validate_bar(prices, volume=volume, amount=amount, trading=trade_status)
        normalized.append(
            {
                "trade_date": trade_date,
                "code": _required(row, "code"),
                **prices,
                "volume": volume,
                "amount": amount,
                "adjustment_flag": _required(row, "adjustflag"),
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
        normalized.append(
            {
                "code": _required(row, "code"),
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


def normalize_five_minute_bars(
    batch: ProviderBatch, artifact: RawArtifact
) -> list[NormalizedRow]:
    normalized = []
    for row in batch.rows:
        trade_date = parse_date(_required(row, "date"), field="date")
        assert trade_date is not None
        timestamp = parse_baostock_bar_time(_required(row, "time"))
        prices = {
            "open": _decimal(_required(row, "open"), field="open"),
            "high": _decimal(_required(row, "high"), field="high"),
            "low": _decimal(_required(row, "low"), field="low"),
            "close": _decimal(_required(row, "close"), field="close"),
        }
        volume = _integer(_required(row, "volume"), field="volume")
        amount = _decimal(_required(row, "amount"), field="amount")
        _validate_bar(prices, volume=volume, amount=amount, trading=True)
        normalized.append(
            {
                "timestamp": timestamp,
                "trade_date": trade_date,
                "code": _required(row, "code"),
                **prices,
                "volume": volume,
                "amount": amount,
                "adjustment_flag": _required(row, "adjustflag"),
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
    "adjustment_factors": ("adjustment_factors", normalize_adjustment_factors),
    "five_minute_bars": ("five_minute_bars", normalize_five_minute_bars),
}


def normalize_batch(
    batch: ProviderBatch, artifact: RawArtifact
) -> tuple[str, list[NormalizedRow]]:
    try:
        dataset, normalizer = NORMALIZERS[batch.operation]
    except KeyError as exc:
        raise DataValidationError(f"unsupported provider operation: {batch.operation}") from exc
    return dataset, normalizer(batch, artifact)
