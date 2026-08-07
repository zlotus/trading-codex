from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from trading_codex.data.models import DataValidationError

SHANGHAI = ZoneInfo("Asia/Shanghai")


def require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def parse_date(value: str, *, field: str, optional: bool = False) -> date | None:
    if value == "" and optional:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DataValidationError(f"invalid {field}: {value!r}") from exc


def shanghai_at(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=SHANGHAI).astimezone(UTC)


def parse_baostock_bar_time(value: str) -> datetime:
    try:
        local = datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        raise DataValidationError(f"invalid BaoStock bar time: {value!r}") from exc
    return local.astimezone(UTC)
