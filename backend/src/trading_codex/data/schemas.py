from dataclasses import dataclass

import pyarrow as pa


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    schema: pa.Schema
    keys: tuple[str, ...]


PROVENANCE_FIELDS = [
    pa.field("available_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("source_received_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("source_payload_sha256", pa.string(), nullable=False),
    pa.field("raw_artifact", pa.string(), nullable=False),
]

PRICE = pa.decimal128(20, 6)
AMOUNT = pa.decimal128(24, 4)
RATIO = pa.decimal128(20, 8)


def _schema(fields: list[pa.Field]) -> pa.Schema:
    return pa.schema([*fields, *PROVENANCE_FIELDS])


DATASET_SPECS = {
    "instruments": DatasetSpec(
        name="instruments",
        schema=_schema(
            [
                pa.field("code", pa.string(), nullable=False),
                pa.field("name", pa.string(), nullable=False),
                pa.field("ipo_date", pa.date32(), nullable=False),
                pa.field("out_date", pa.date32()),
                pa.field("security_type", pa.string(), nullable=False),
                pa.field("status", pa.string(), nullable=False),
            ]
        ),
        keys=("code",),
    ),
    "trade_calendar": DatasetSpec(
        name="trade_calendar",
        schema=_schema(
            [
                pa.field("calendar_date", pa.date32(), nullable=False),
                pa.field("is_trading_day", pa.bool_(), nullable=False),
            ]
        ),
        keys=("calendar_date",),
    ),
    "historical_universe": DatasetSpec(
        name="historical_universe",
        schema=_schema(
            [
                pa.field("snapshot_date", pa.date32(), nullable=False),
                pa.field("code", pa.string(), nullable=False),
                pa.field("name", pa.string(), nullable=False),
                pa.field("trade_status", pa.bool_(), nullable=False),
            ]
        ),
        keys=("snapshot_date", "code"),
    ),
    "index_memberships": DatasetSpec(
        name="index_memberships",
        schema=_schema(
            [
                pa.field("snapshot_date", pa.date32(), nullable=False),
                pa.field("index_code", pa.string(), nullable=False),
                pa.field("member_code", pa.string(), nullable=False),
                pa.field("member_name", pa.string(), nullable=False),
            ]
        ),
        keys=("snapshot_date", "index_code", "member_code"),
    ),
    "daily_bars": DatasetSpec(
        name="daily_bars",
        schema=_schema(
            [
                pa.field("trade_date", pa.date32(), nullable=False),
                pa.field("code", pa.string(), nullable=False),
                pa.field("open", PRICE),
                pa.field("high", PRICE),
                pa.field("low", PRICE),
                pa.field("close", PRICE),
                pa.field("previous_close", PRICE),
                pa.field("volume", pa.int64(), nullable=False),
                pa.field("amount", AMOUNT),
                pa.field("adjustment_flag", pa.string(), nullable=False),
                pa.field("turnover", RATIO),
                pa.field("trade_status", pa.bool_(), nullable=False),
                pa.field("pct_change", RATIO),
                pa.field("is_st", pa.bool_(), nullable=False),
            ]
        ),
        keys=("trade_date", "code", "adjustment_flag"),
    ),
    "adjustment_factors": DatasetSpec(
        name="adjustment_factors",
        schema=_schema(
            [
                pa.field("code", pa.string(), nullable=False),
                pa.field("effective_date", pa.date32(), nullable=False),
                pa.field("forward_factor", RATIO, nullable=False),
                pa.field("backward_factor", RATIO, nullable=False),
                pa.field("adjustment_factor", RATIO, nullable=False),
            ]
        ),
        keys=("code", "effective_date"),
    ),
    "corporate_actions": DatasetSpec(
        name="corporate_actions",
        schema=_schema(
            [
                pa.field("action_id", pa.string(), nullable=False),
                pa.field("code", pa.string(), nullable=False),
                pa.field("announcement_date", pa.date32(), nullable=False),
                pa.field("record_date", pa.date32()),
                pa.field("ex_date", pa.date32(), nullable=False),
                pa.field("pay_date", pa.date32()),
                pa.field("cash_before_tax_per_share", RATIO, nullable=False),
                pa.field("stock_dividend_ratio", RATIO, nullable=False),
                pa.field("capitalization_ratio", RATIO, nullable=False),
            ]
        ),
        keys=("action_id",),
    ),
    "five_minute_bars": DatasetSpec(
        name="five_minute_bars",
        schema=_schema(
            [
                pa.field("timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
                pa.field("trade_date", pa.date32(), nullable=False),
                pa.field("code", pa.string(), nullable=False),
                pa.field("open", PRICE, nullable=False),
                pa.field("high", PRICE, nullable=False),
                pa.field("low", PRICE, nullable=False),
                pa.field("close", PRICE, nullable=False),
                pa.field("volume", pa.int64(), nullable=False),
                pa.field("amount", AMOUNT, nullable=False),
                pa.field("adjustment_flag", pa.string(), nullable=False),
            ]
        ),
        keys=("timestamp", "code", "adjustment_flag"),
    ),
}
