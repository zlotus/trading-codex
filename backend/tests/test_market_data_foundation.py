from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from trading_codex.data.models import (
    DataValidationError,
    FutureDataError,
    ProviderBatch,
    RawIntegrityError,
)
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.quality import assess_opening_0935_coverage, inspect_data_quality
from trading_codex.data.raw_store import ImmutableRawStore
from trading_codex.data.sync import BaoStockSyncService, IngestionPipeline

RECEIVED_AT = datetime(2024, 1, 4, 8, tzinfo=UTC)


def _batch(
    operation: str,
    fields: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    *,
    query: dict[str, str] | None = None,
    received_at: datetime = RECEIVED_AT,
) -> ProviderBatch:
    return ProviderBatch(
        source="baostock",
        operation=operation,
        query=query or {},
        fields=fields,
        rows=tuple(dict(zip(fields, values, strict=True)) for values in rows),
        received_at=received_at,
    )


class FixedBaoStockClient:
    def __enter__(self) -> "FixedBaoStockClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def instruments(self, *, code: str = "") -> ProviderBatch:
        assert code == ""
        return _batch(
            "instruments",
            ("code", "code_name", "ipoDate", "outDate", "type", "status"),
            (("sh.600000", "浦发银行", "1999-11-10", "", "1", "1"),),
            query={"code": code},
        )

    def trade_calendar(self, *, start_date: date, end_date: date) -> ProviderBatch:
        rows = tuple(
            row
            for row in (("2024-01-02", "1"), ("2024-01-03", "0"))
            if start_date <= date.fromisoformat(row[0]) <= end_date
        )
        return _batch(
            "trade_calendar",
            ("calendar_date", "is_trading_day"),
            rows,
            query={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )

    def historical_universe(self, *, day: date) -> ProviderBatch:
        return _batch(
            "historical_universe",
            ("code", "tradeStatus", "code_name"),
            (("sh.600000", "1", "浦发银行"),),
            query={"day": day.isoformat()},
        )

    def daily_bars(
        self,
        *,
        code: str,
        start_date: date,
        end_date: date,
        adjustment_flag: str = "3",
    ) -> ProviderBatch:
        return _batch(
            "daily_bars",
            (
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
            ),
            (
                (
                    "2024-01-02",
                    code,
                    "6.6300",
                    "6.6500",
                    "6.6000",
                    "6.6000",
                    "6.6200",
                    "22066700",
                    "146066303.7200",
                    adjustment_flag,
                    "0.075200",
                    "1",
                    "-0.302100",
                    "0",
                ),
            ),
            query={
                "code": code,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "frequency": "d",
                "adjustflag": adjustment_flag,
            },
        )

    def adjustment_factors(
        self, *, code: str, start_date: date, end_date: date
    ) -> ProviderBatch:
        return _batch(
            "adjustment_factors",
            (
                "code",
                "dividOperateDate",
                "foreAdjustFactor",
                "backAdjustFactor",
                "adjustFactor",
            ),
            ((code, "2024-01-02", "0.900000", "11.000000", "11.000000"),),
            query={
                "code": code,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )

    def five_minute_bars(
        self, *, code: str, start_date: date, end_date: date
    ) -> ProviderBatch:
        return _batch(
            "five_minute_bars",
            (
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
            ),
            (
                (
                    "2024-01-02",
                    "20240102093500000",
                    code,
                    "6.6300",
                    "6.6400",
                    "6.6100",
                    "6.6200",
                    "1902300",
                    "12603192.0000",
                    "3",
                ),
            ),
            query={
                "code": code,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "frequency": "5",
                "adjustflag": "3",
            },
        )


class IncompleteCalendarClient(FixedBaoStockClient):
    def trade_calendar(self, *, start_date: date, end_date: date) -> ProviderBatch:
        return _batch(
            "trade_calendar",
            ("calendar_date", "is_trading_day"),
            (("2024-01-02", "1"),),
            query={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )


class EmptyUniverseClient(FixedBaoStockClient):
    def historical_universe(self, *, day: date) -> ProviderBatch:
        return _batch(
            "historical_universe",
            ("code", "tradeStatus", "code_name"),
            (),
            query={"day": day.isoformat()},
        )


class EmptyDailyBarsClient(FixedBaoStockClient):
    def daily_bars(
        self,
        *,
        code: str,
        start_date: date,
        end_date: date,
        adjustment_flag: str = "3",
    ) -> ProviderBatch:
        batch = super().daily_bars(
            code=code,
            start_date=start_date,
            end_date=end_date,
            adjustment_flag=adjustment_flag,
        )
        return _batch(
            "daily_bars",
            batch.fields,
            (),
            query=batch.query,
        )


def _pipeline(tmp_path: Path) -> tuple[IngestionPipeline, ParquetDataStore, ImmutableRawStore]:
    raw = ImmutableRawStore(tmp_path / "raw")
    normalized = ParquetDataStore(tmp_path / "normalized")
    return IngestionPipeline(raw, normalized), normalized, raw


def test_sync_is_idempotent_and_preserves_provenance(tmp_path: Path) -> None:
    pipeline, store, _ = _pipeline(tmp_path)
    service = BaoStockSyncService(FixedBaoStockClient(), pipeline)

    first = service.sync(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        codes=["sh.600000", "sh.600000"],
        include_five_minute_bars=True,
    )
    second = service.sync(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        codes=["sh.600000"],
        include_five_minute_bars=True,
    )

    assert any(result.inserted for result in first.results)
    assert all(not result.changed for result in second.results)
    assert all(result.updated == 0 for result in second.results)
    for dataset in (
        "instruments",
        "trade_calendar",
        "historical_universe",
        "daily_bars",
        "adjustment_factors",
        "five_minute_bars",
    ):
        rows = store.read(dataset).to_pylist()
        assert rows
        assert all(row["source"] == "baostock" for row in rows)
        assert all(row["source_received_at"] == RECEIVED_AT for row in rows)
        assert all(row["source_payload_sha256"] for row in rows)
        assert all(row["raw_artifact"].endswith(".json") for row in rows)


def test_forward_adjusted_daily_sync_is_explicit_and_idempotent(tmp_path: Path) -> None:
    pipeline, store, _ = _pipeline(tmp_path)
    service = BaoStockSyncService(FixedBaoStockClient(), pipeline)

    first = service.sync(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        codes=["sh.600000"],
        include_forward_adjusted_daily=True,
    )
    second = service.sync(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        codes=["sh.600000"],
        include_forward_adjusted_daily=True,
    )

    assert first.included_forward_adjusted_daily is True
    assert second.included_forward_adjusted_daily is True
    assert {row["adjustment_flag"] for row in store.read("daily_bars").to_pylist()} == {
        "2",
        "3",
    }
    assert all(result.inserted == 0 and result.updated == 0 for result in second.results)


def test_sync_rejects_incomplete_calendar(tmp_path: Path) -> None:
    pipeline, _, _ = _pipeline(tmp_path)

    with pytest.raises(DataValidationError, match="calendar is incomplete"):
        BaoStockSyncService(IncompleteCalendarClient(), pipeline).sync(
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 3),
            codes=["sh.600000"],
        )


def test_sync_rejects_empty_historical_universe(tmp_path: Path) -> None:
    pipeline, _, _ = _pipeline(tmp_path)

    with pytest.raises(DataValidationError, match="universe is empty"):
        BaoStockSyncService(EmptyUniverseClient(), pipeline).sync(
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 2),
            codes=["sh.600000"],
        )


def test_sync_rejects_missing_expected_daily_bars(tmp_path: Path) -> None:
    pipeline, _, _ = _pipeline(tmp_path)

    with pytest.raises(DataValidationError, match="daily bars are incomplete"):
        BaoStockSyncService(EmptyDailyBarsClient(), pipeline).sync(
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 2),
            codes=["sh.600000"],
        )


def test_raw_store_uses_content_address_and_detects_tampering(tmp_path: Path) -> None:
    raw = ImmutableRawStore(tmp_path / "raw")
    original = FixedBaoStockClient().instruments()
    later = _batch(
        original.operation,
        original.fields,
        tuple(tuple(row[field] for field in original.fields) for row in original.rows),
        query=original.query,
        received_at=RECEIVED_AT + timedelta(days=1),
    )

    first = raw.persist(original)
    second = raw.persist(later)

    assert first.path == second.path
    assert second.received_at == RECEIVED_AT
    assert len(list((tmp_path / "raw" / "baostock").rglob("*.json"))) == 1

    first.path.write_text("{}", encoding="utf-8")
    with pytest.raises(RawIntegrityError):
        raw.persist(original)


def test_point_in_time_view_filters_and_rejects_future_requests(tmp_path: Path) -> None:
    pipeline, store, _ = _pipeline(tmp_path)
    pipeline.ingest(FixedBaoStockClient().daily_bars(
        code="sh.600000", start_date=date(2024, 1, 2), end_date=date(2024, 1, 3)
    ))

    before_close = datetime(2024, 1, 2, 6, 59, tzinfo=UTC)
    at_close = datetime(2024, 1, 2, 7, 0, tzinfo=UTC)
    assert store.rows_as_of("daily_bars", as_of=before_close) == []
    assert len(store.rows_as_of("daily_bars", as_of=at_close)) == 1

    with pytest.raises(FutureDataError):
        store.daily_bars(
            codes=["sh.600000"],
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 3),
            as_of=at_close,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        store.rows_as_of("daily_bars", as_of=datetime(2024, 1, 2, 15, 0))


def test_bounded_scan_pushes_daily_filters_and_projection(tmp_path: Path) -> None:
    pipeline, store, _ = _pipeline(tmp_path)
    pipeline.ingest(
        FixedBaoStockClient().daily_bars(
            code="sh.600000",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 2),
        )
    )

    table = store.scan(
        "daily_bars",
        as_of=datetime(2024, 1, 2, 7, tzinfo=UTC),
        columns=("trade_date", "code", "close"),
        contained_in={"code": ("sh.600000",)},
        ranges={"trade_date": (date(2024, 1, 2), date(2024, 1, 2))},
    )
    series = store.daily_bar_series(
        codes=("sh.600000",),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
        as_of=datetime(2024, 1, 2, 7, tzinfo=UTC),
        adjustment_flags=("3",),
        columns=("trade_date", "code", "adjustment_flag", "close"),
    )

    assert table.column_names == ["trade_date", "code", "close"]
    assert table.num_rows == 1
    assert tuple(series) == (("sh.600000", "3"),)
    assert series[("sh.600000", "3")].column_names == [
        "trade_date",
        "code",
        "adjustment_flag",
        "close",
    ]


def test_invalid_ohlc_fails_closed_before_normalized_write(tmp_path: Path) -> None:
    pipeline, store, raw = _pipeline(tmp_path)
    valid = FixedBaoStockClient().daily_bars(
        code="sh.600000", start_date=date(2024, 1, 2), end_date=date(2024, 1, 2)
    )
    values = [tuple(row[field] for field in valid.fields) for row in valid.rows]
    high_index = valid.fields.index("high")
    invalid_values = list(values[0])
    invalid_values[high_index] = "6.6100"
    invalid = _batch(
        "daily_bars",
        valid.fields,
        (tuple(invalid_values),),
        query=valid.query,
    )

    with pytest.raises(DataValidationError, match="OHLC"):
        pipeline.ingest(invalid)

    assert store.read("daily_bars").num_rows == 0
    assert len(list((raw.root / "baostock").rglob("*.json"))) == 1


def test_quality_and_0935_coverage_reports_pass_for_consistent_data(tmp_path: Path) -> None:
    pipeline, store, _ = _pipeline(tmp_path)
    BaoStockSyncService(FixedBaoStockClient(), pipeline).sync(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        codes=["sh.600000"],
        include_five_minute_bars=True,
    )

    quality = inspect_data_quality(store, as_of=RECEIVED_AT, generated_at=RECEIVED_AT)
    coverage = assess_opening_0935_coverage(
        store,
        codes=["sh.600000"],
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        as_of=RECEIVED_AT,
        generated_at=RECEIVED_AT,
    )

    assert quality.status == "passed"
    assert quality.issues == ()
    assert coverage.status == "passed"
    assert coverage.calendar_complete is True
    assert coverage.universe_complete is True
    assert coverage.expected_code_days == 1
    assert coverage.covered_code_days == 1
    assert coverage.coverage_ratio == 1.0
    assert coverage.missing_calendar_dates == ()
    assert coverage.missing_universe_dates == ()
    assert coverage.missing == ()


def test_0935_coverage_fails_closed_on_incomplete_prerequisites(tmp_path: Path) -> None:
    pipeline, store, _ = _pipeline(tmp_path)
    client = FixedBaoStockClient()
    pipeline.ingest(
        client.trade_calendar(
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 3)
        )
    )

    missing_universe = assess_opening_0935_coverage(
        store,
        codes=["sh.600000"],
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        as_of=RECEIVED_AT,
        generated_at=RECEIVED_AT,
    )
    missing_calendar = assess_opening_0935_coverage(
        store,
        codes=["sh.600000"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        as_of=RECEIVED_AT,
        generated_at=RECEIVED_AT,
    )

    assert missing_universe.status == "failed"
    assert missing_universe.calendar_complete is True
    assert missing_universe.universe_complete is False
    assert missing_universe.missing_universe_dates == ("2024-01-02",)
    assert missing_universe.missing == ("2024-01-02:sh.600000",)
    assert missing_calendar.status == "failed"
    assert missing_calendar.calendar_complete is False
    assert missing_calendar.missing_calendar_dates == ("2024-01-01",)
