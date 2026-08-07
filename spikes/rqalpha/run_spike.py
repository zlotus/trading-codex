import json
import tempfile
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from rqalpha import run_func
from rqalpha.api import get_position, order_shares, update_universe

from trading_codex.backtest.rqalpha_data_source import (
    RQAlphaParquetDataSource,
    baostock_to_order_book_id,
)
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.time import SHANGHAI

CODES = (
    "sh.600000",
    "sh.600519",
    "sh.601398",
    "sh.601318",
    "sh.600036",
    "sh.600030",
    "sh.600276",
    "sh.601888",
    "sh.603259",
    "sh.688981",
    "sz.000001",
    "sz.000333",
    "sz.000651",
    "sz.000858",
    "sz.002594",
    "sz.002415",
    "sz.300750",
    "sz.300059",
    "sz.301269",
    "bj.430047",
)
TRADING_DAYS = (
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
)
AS_OF = datetime(2024, 1, 8, 16, tzinfo=SHANGHAI).astimezone(UTC)
PRIMARY = "600000.XSHG"
FEE_CODE = "600519.XSHG"
SUSPENDED = "000651.XSHE"
LIMIT_DOWN = "000858.XSHE"
LIMIT_UP = "300750.XSHE"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="trading-codex-rqalpha-") as temporary:
        root = Path(temporary)
        normalized = root / "normalized"
        _build_fixture(ParquetDataStore(normalized))
        adapter = RQAlphaParquetDataSource(normalized, as_of=AS_OF)
        assert len(list(adapter.get_instruments())) == 20

        observations: dict[str, Any] = {}

        def init(context: Any) -> None:
            update_universe([baostock_to_order_book_id(code) for code in CODES])

        def handle_bar(context: Any, bar_dict: Any) -> None:
            current = context.now.date()
            if current == TRADING_DAYS[0]:
                lot_order = order_shares(PRIMARY, 150)
                observations["rounded_order_quantity"] = lot_order.quantity
                observations["bought_quantity"] = get_position(PRIMARY).quantity
                observations["same_day_sell_rejected"] = (
                    order_shares(PRIMARY, -100) is None
                )
                order_shares(FEE_CODE, 100)
                observations["suspension_rejected"] = order_shares(SUSPENDED, 100) is None
                limit_up_order = order_shares(LIMIT_UP, 100)
                observations["limit_up_status"] = limit_up_order.status.name
                limit_down_order = order_shares(LIMIT_DOWN, -100)
                observations["limit_down_status"] = limit_down_order.status.name
            elif current == TRADING_DAYS[1]:
                observations["next_day_closable"] = get_position(PRIMARY).closable
                order_shares(FEE_CODE, -100)
            elif current == TRADING_DAYS[2]:
                position = get_position(PRIMARY)
                observations["post_split_quantity"] = position.quantity
                observations["post_split_avg_price"] = position.avg_price
                order_shares(PRIMARY, -position.closable)

        result = run_func(
            config=_rqalpha_config(root, normalized),
            init=init,
            handle_bar=handle_bar,
        )
        analyser = result["sys_analyser"]
        trades = analyser["trades"]
        fee_trades = trades[trades["order_book_id"] == FEE_CODE]
        observations["fee_trade_count"] = len(fee_trades)
        observations["fee_transaction_cost"] = float(
            fee_trades["transaction_cost"].sum()
        )
        observations["instrument_count"] = len(list(adapter.get_instruments()))

        expected = {
            "rounded_order_quantity": 100,
            "bought_quantity": 100,
            "same_day_sell_rejected": True,
            "next_day_closable": 100,
            "suspension_rejected": True,
            "limit_up_status": "REJECTED",
            "limit_down_status": "REJECTED",
            "post_split_quantity": 200,
            "post_split_avg_price": 5.0,
            "fee_trade_count": 2,
            "fee_transaction_cost": 11.0,
            "instrument_count": 20,
        }
        failures = {
            key: {"expected": value, "actual": observations.get(key)}
            for key, value in expected.items()
            if not _equivalent(observations.get(key), value)
        }
        payload = {
            "rqalpha_version": _rqalpha_version(),
            "status": "passed" if not failures else "failed",
            "observations": observations,
            "failures": failures,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        if failures:
            raise SystemExit(1)


def _build_fixture(store: ParquetDataStore) -> None:
    provenance = {
        "source": "rqalpha_fixture",
        "source_received_at": AS_OF,
        "source_payload_sha256": "fixture-v1",
        "raw_artifact": "rqalpha-fixture-v1.json",
    }
    store.merge(
        "instruments",
        [
            {
                "code": code,
                "name": f"Fixture {index:02d}",
                "ipo_date": date(2000, 1, 1),
                "out_date": None,
                "security_type": "1",
                "status": "1",
                "available_at": datetime(2000, 1, 1, tzinfo=UTC),
                **provenance,
            }
            for index, code in enumerate(CODES)
        ],
    )
    store.merge(
        "trade_calendar",
        [
            {
                "calendar_date": day,
                "is_trading_day": True,
                "available_at": datetime.combine(day, time.min, tzinfo=SHANGHAI).astimezone(
                    UTC
                ),
                **provenance,
            }
            for day in TRADING_DAYS
        ],
    )
    store.merge(
        "historical_universe",
        [
            {
                "snapshot_date": day,
                "code": code,
                "name": f"Fixture {index:02d}",
                "trade_status": not (code == "sz.000651" and day == TRADING_DAYS[0]),
                "available_at": datetime.combine(day, time(9), tzinfo=SHANGHAI).astimezone(
                    UTC
                ),
                **provenance,
            }
            for day in TRADING_DAYS
            for index, code in enumerate(CODES)
        ],
    )
    store.merge("daily_bars", _daily_bars(provenance))
    store.merge(
        "corporate_actions",
        [
            {
                "action_id": "fixture-split-600000-20240104",
                "code": "sh.600000",
                "announcement_date": date(2023, 12, 20),
                "record_date": date(2024, 1, 3),
                "ex_date": date(2024, 1, 4),
                "pay_date": date(2024, 1, 4),
                "cash_before_tax_per_share": Decimal("0"),
                "stock_dividend_ratio": Decimal("1"),
                "capitalization_ratio": Decimal("0"),
                "available_at": datetime(2023, 12, 20, 15, tzinfo=SHANGHAI).astimezone(
                    UTC
                ),
                **provenance,
            }
        ],
    )


def _daily_bars(provenance: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, code in enumerate(CODES):
        previous = Decimal(10 + index)
        for day_index, day in enumerate(TRADING_DAYS):
            price = previous
            trade_status = True
            if code == "sh.600000":
                prices = [
                    Decimal("10"),
                    Decimal("10"),
                    Decimal("5.1"),
                    Decimal("5.2"),
                    Decimal("5.3"),
                ]
                previous_closes = [
                    Decimal("9.8"),
                    Decimal("10"),
                    Decimal("5"),
                    Decimal("5.1"),
                    Decimal("5.2"),
                ]
                price = prices[day_index]
                previous = previous_closes[day_index]
            elif code == "sh.600519":
                price = Decimal("20")
                previous = Decimal("20")
            elif code == "sz.000651" and day == TRADING_DAYS[0]:
                trade_status = False
            elif code == "sz.000858" and day == TRADING_DAYS[0]:
                previous = Decimal("10")
                price = Decimal("9")
            elif code == "sz.300750" and day == TRADING_DAYS[0]:
                previous = Decimal("10")
                price = Decimal("12")

            rows.append(
                {
                    "trade_date": day,
                    "code": code,
                    "open": price if trade_status else None,
                    "high": price if trade_status else None,
                    "low": price if trade_status else None,
                    "close": price if trade_status else None,
                    "previous_close": previous,
                    "volume": 100_000 if trade_status else 0,
                    "amount": price * 100_000 if trade_status else None,
                    "adjustment_flag": "3",
                    "turnover": Decimal("1"),
                    "trade_status": trade_status,
                    "pct_change": Decimal("0"),
                    "is_st": False,
                    "available_at": datetime.combine(day, time(15), tzinfo=SHANGHAI).astimezone(
                        UTC
                    ),
                    **provenance,
                }
            )
            previous = price
    return rows


def _rqalpha_config(root: Path, normalized: Path) -> dict[str, Any]:
    return {
        "base": {
            "start_date": TRADING_DAYS[0].isoformat(),
            "end_date": TRADING_DAYS[-1].isoformat(),
            "frequency": "1d",
            "accounts": {"stock": 1_000_000},
            "init_positions": f"{LIMIT_DOWN}:100",
            "data_bundle_path": str(root / "unused-bundle"),
            "capital_gain_tax_rate": 0,
        },
        "extra": {"log_level": "error"},
        "mod": {
            "trading_codex_data": {
                "enabled": True,
                "lib": "trading_codex.backtest.rqalpha_mod",
                "priority": 0,
                "normalized_root": str(normalized),
                "as_of": AS_OF.isoformat(),
            },
            "sys_progress": {"enabled": False},
            "sys_analyser": {"enabled": True, "record": True, "benchmark": None},
            "sys_simulation": {
                "enabled": True,
                "matching_type": "current_bar",
                "volume_limit": False,
                "price_limit": True,
                "inactive_limit": True,
            },
            "sys_transaction_cost": {
                "enabled": True,
                "stock_commission_multiplier": 0.375,
                "stock_min_commission": 5,
                "tax_multiplier": 1,
                "pit_tax": True,
            },
        },
    }


def _rqalpha_version() -> str:
    import rqalpha

    return rqalpha.__version__


def _equivalent(actual: Any, expected: Any) -> bool:
    if isinstance(expected, float):
        return actual is not None and abs(float(actual) - expected) < 1e-9
    return actual == expected


if __name__ == "__main__":
    main()
