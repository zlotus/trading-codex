import importlib
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from trading_codex.baostock_download.endpoints import (
    ADJUSTMENT_FACTOR_FIELDS,
    BULK_DAILY_BAR_FIELDS,
    DIVIDEND_FIELDS,
    INDEX_MEMBERSHIP_FIELDS,
)
from trading_codex.baostock_download.errors import OfflineSyncError
from trading_codex.baostock_download.manifest import create_manifest
from trading_codex.baostock_download.offline import import_raw_cache
from trading_codex.baostock_download.provider import BaoStockProvider
from trading_codex.data.models import DataValidationError, ProviderBatch
from trading_codex.data.normalizers import normalize_batch
from trading_codex.data.raw_store import ImmutableRawStore
from trading_codex.data.time import SHANGHAI


class Result:
    def __init__(self, fields: tuple[str, ...], row: tuple[str, ...]) -> None:
        self.error_code = "0"
        self.error_msg = "success"
        self.fields = list(fields)
        self.row = row
        self.read = False

    def next(self) -> bool:
        return not self.read

    def get_row_data(self) -> list[str]:
        self.read = True
        return list(self.row)


class NewEndpointModule:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def query_daily_history_k_AStock(self, *, date: str) -> Result:
        self.calls.append(("daily_history_astock", {"date": date}))
        values = {
            "date": date,
            "code": "sh.600000",
            "open": "10",
            "high": "11",
            "low": "9",
            "close": "10.5",
            "preclose": "10",
            "volume": "100",
            "amount": "1000",
            "adjustflag": "3",
            "turn": "0.1",
            "tradestatus": "1",
            "pctChg": "5",
            "peTTM": "8",
            "pbMRQ": "1",
            "psTTM": "2",
            "pcfNcfTTM": "3",
            "isST": "0",
        }
        return Result(
            BULK_DAILY_BAR_FIELDS,
            tuple(values[field] for field in BULK_DAILY_BAR_FIELDS),
        )

    def query_daily_adjust_factor(self, *, date: str) -> Result:
        self.calls.append(("daily_adjust_factors", {"date": date}))
        return Result(
            ADJUSTMENT_FACTOR_FIELDS,
            ("sh.600000", date, "0.9", "1.1", "1.1"),
        )

    def query_hs300_stocks(self, *, date: str) -> Result:
        self.calls.append(("hs300_stocks", {"date": date}))
        return Result(INDEX_MEMBERSHIP_FIELDS, (date, "sh.600000", "浦发银行"))

    def query_zz500_stocks(self, *, date: str) -> Result:
        self.calls.append(("zz500_stocks", {"date": date}))
        return Result(INDEX_MEMBERSHIP_FIELDS, (date, "sz.000001", "平安银行"))

    def query_dividend_data(self, *, code: str, year: str, yearType: str) -> Result:
        self.calls.append(
            ("dividends", {"code": code, "year": year, "yearType": yearType})
        )
        values = {
            "code": code,
            "dividPreNoticeDate": "2024-03-01",
            "dividAgmPumDate": "2024-04-01",
            "dividPlanAnnounceDate": "2024-03-01",
            "dividPlanDate": "2024-05-01",
            "dividRegistDate": "2024-05-09",
            "dividOperateDate": "2024-05-10",
            "dividPayDate": "2024-05-10",
            "dividStockMarketDate": "",
            "dividCashPsBeforeTax": "0.30",
            "dividCashPsAfterTax": "0.27",
            "dividStocksPs": "0.10",
            "dividCashStock": "A股",
            "dividReserveToStockPs": "0.20",
        }
        return Result(DIVIDEND_FIELDS, tuple(values[field] for field in DIVIDEND_FIELDS))


def _new_endpoint_manifest():
    return create_manifest(
        {
            "created_by": "test",
            "items": [
                {
                    "operation": "daily_history_astock",
                    "query": {"date": "2024-05-10"},
                },
                {
                    "operation": "daily_adjust_factors",
                    "query": {"date": "2024-05-10"},
                },
                {
                    "operation": "hs300_stocks",
                    "query": {"date": "2024-05-10"},
                },
                {
                    "operation": "zz500_stocks",
                    "query": {"date": "2024-05-10"},
                },
                {
                    "operation": "dividends",
                    "query": {
                        "code": "sh.600000",
                        "year": "2024",
                        "yearType": "report",
                    },
                },
            ],
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    ).frozen()


def test_new_endpoint_adapter_and_normalizers_have_fixed_contracts(tmp_path: Path) -> None:
    manifest = _new_endpoint_manifest()
    module = NewEndpointModule()
    provider = BaoStockProvider(
        module,
        clock=lambda: datetime(2024, 5, 11, tzinfo=UTC),
    )
    provider.logged_in = True
    raw = ImmutableRawStore(tmp_path / "raw")
    normalized = {}

    for item in manifest.items:
        batch = provider.fetch(item)
        assert batch.fields == item.expected_fields
        artifact = raw.persist(batch)
        dataset, rows = normalize_batch(batch, artifact)
        normalized.setdefault(dataset, []).extend(rows)

    assert [name for name, _ in module.calls] == [
        "daily_history_astock",
        "daily_adjust_factors",
        "hs300_stocks",
        "zz500_stocks",
        "dividends",
    ]
    assert normalized["daily_bars"][0]["adjustment_flag"] == "3"
    assert normalized["adjustment_factors"][0]["effective_date"].isoformat() == "2024-05-10"
    assert {row["index_code"] for row in normalized["index_memberships"]} == {
        "sh.000300",
        "sh.000905",
    }
    action = normalized["corporate_actions"][0]
    assert str(action["cash_before_tax_per_share"]) == "0.30"
    assert str(action["stock_dividend_ratio"]) == "0.10"
    assert str(action["capitalization_ratio"]) == "0.20"
    assert action["available_at"].astimezone(SHANGHAI).date() == date(2024, 3, 2)


def test_bulk_daily_normalizer_rejects_unknown_adjustment_track(tmp_path: Path) -> None:
    item = _new_endpoint_manifest().items[0]
    module = NewEndpointModule()
    provider = BaoStockProvider(
        module,
        clock=lambda: datetime(2024, 5, 11, tzinfo=UTC),
    )
    provider.logged_in = True
    batch = provider.fetch(item)
    row = {**batch.rows[0], "adjustflag": "0"}
    drifted = ProviderBatch(
        source=batch.source,
        operation=batch.operation,
        query=batch.query,
        fields=batch.fields,
        rows=(row,),
        received_at=batch.received_at,
    )
    artifact = ImmutableRawStore(tmp_path / "raw").persist(drifted)

    with pytest.raises(DataValidationError, match="adjustment flag"):
        normalize_batch(drifted, artifact)


@pytest.mark.parametrize(
    ("item_index", "field", "value", "match"),
    [
        (0, "date", "2024-05-11", "exact query date"),
        (1, "dividOperateDate", "2024-05-11", "exact query date"),
        (4, "code", "sz.000001", "exact query code"),
    ],
)
def test_exact_query_normalizers_reject_misattributed_provider_rows(
    tmp_path: Path,
    item_index: int,
    field: str,
    value: str,
    match: str,
) -> None:
    item = _new_endpoint_manifest().items[item_index]
    module = NewEndpointModule()
    provider = BaoStockProvider(
        module,
        clock=lambda: datetime(2024, 5, 11, tzinfo=UTC),
    )
    provider.logged_in = True
    batch = provider.fetch(item)
    drifted = ProviderBatch(
        source=batch.source,
        operation=batch.operation,
        query=batch.query,
        fields=batch.fields,
        rows=({**batch.rows[0], field: value},),
        received_at=batch.received_at,
    )
    artifact = ImmutableRawStore(tmp_path / "raw").persist(drifted)

    with pytest.raises(DataValidationError, match=match):
        normalize_batch(drifted, artifact)


def test_import_raw_requires_and_records_exact_provider_client_version(tmp_path: Path) -> None:
    source = ImmutableRawStore(tmp_path / "old-raw")
    source.persist(
        ProviderBatch(
            source="baostock",
            operation="trade_calendar",
            query={"start_date": "2024-01-01", "end_date": "2024-01-02"},
            fields=("calendar_date", "is_trading_day"),
            rows=({"calendar_date": "2024-01-02", "is_trading_day": "1"},),
            received_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
    )
    data_root = tmp_path / "data"

    with pytest.raises(OfflineSyncError, match="00.9.30"):
        import_raw_cache(
            source_root=source.root,
            data_root=data_root,
            source_provider_client_version="00.8.90",
        )

    report = import_raw_cache(
        source_root=source.root,
        data_root=data_root,
        source_provider_client_version="00.9.30",
    )
    imported = ImmutableRawStore(data_root / "raw").lookup(
        source="baostock",
        operation="trade_calendar",
        query={
            "start_date": "2024-01-01",
            "end_date": "2024-01-02",
            "_provider_client_version": "00.9.30",
        },
    )
    assert report["imported"] == 1
    assert imported is not None


def test_import_raw_verifies_every_artifact_and_keeps_latest_exact_query(
    tmp_path: Path,
) -> None:
    source = ImmutableRawStore(tmp_path / "old-raw")
    query = {"start_date": "2024-01-01", "end_date": "2024-01-02"}
    first = source.persist(
        ProviderBatch(
            source="baostock",
            operation="trade_calendar",
            query=query,
            fields=("calendar_date", "is_trading_day"),
            rows=({"calendar_date": "2024-01-02", "is_trading_day": "0"},),
            received_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
    )
    source.persist(
        ProviderBatch(
            source="baostock",
            operation="trade_calendar",
            query=query,
            fields=("calendar_date", "is_trading_day"),
            rows=({"calendar_date": "2024-01-02", "is_trading_day": "1"},),
            received_at=datetime(2024, 1, 4, tzinfo=UTC),
        )
    )
    data_root = tmp_path / "data"
    report = import_raw_cache(
        source_root=source.root,
        data_root=data_root,
        source_provider_client_version="00.9.30",
    )
    imported = ImmutableRawStore(data_root / "raw").lookup(
        source="baostock",
        operation="trade_calendar",
        query={**query, "_provider_client_version": "00.9.30"},
    )

    assert report["imported"] == 2
    assert imported is not None
    assert imported.rows[0]["is_trading_day"] == "1"

    first.path.write_bytes(b"tampered\n")
    with pytest.raises(OfflineSyncError, match="integrity verification"):
        import_raw_cache(
            source_root=source.root,
            data_root=tmp_path / "second-data",
            source_provider_client_version="00.9.30",
        )


def test_offline_cli_import_does_not_load_any_provider_module() -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import sys; import trading_codex.baostock_download.cli; "
            "assert 'baostock' not in sys.modules; "
            "assert 'trading_codex.baostock_download.provider' not in sys.modules"
        ),
    ]
    environment = {**os.environ, "PYTHONPATH": "backend/src"}
    result = subprocess.run(
        command,
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cli_module_remains_offline_after_reload() -> None:
    sys.modules.pop("trading_codex.baostock_download.provider", None)
    cli = importlib.import_module("trading_codex.baostock_download.cli")
    importlib.reload(cli)
    assert "trading_codex.baostock_download.provider" not in sys.modules


def test_only_fetch_branch_can_import_the_online_adapter() -> None:
    source = (
        Path(__file__).parents[1]
        / "src/trading_codex/baostock_download/cli.py"
    ).read_text(encoding="utf-8")
    online_import = (
        "from trading_codex.baostock_download.online import fetch_manifest"
    )
    fetch_branch = source.split('if args.command == "fetch":', 1)[1].split(
        'if args.command == "sync":', 1
    )[0]

    assert source.count(online_import) == 1
    assert online_import in fetch_branch
