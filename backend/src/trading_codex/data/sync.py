from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol, Self

from trading_codex.data.models import DataValidationError, MergeResult, ProviderBatch
from trading_codex.data.normalizers import normalize_batch
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.raw_store import ImmutableRawStore


class MarketDataClient(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def instruments(self, *, code: str = "") -> ProviderBatch: ...

    def trade_calendar(self, *, start_date: date, end_date: date) -> ProviderBatch: ...

    def historical_universe(self, *, day: date) -> ProviderBatch: ...

    def daily_bars(
        self, *, code: str, start_date: date, end_date: date
    ) -> ProviderBatch: ...

    def adjustment_factors(
        self, *, code: str, start_date: date, end_date: date
    ) -> ProviderBatch: ...

    def five_minute_bars(
        self, *, code: str, start_date: date, end_date: date
    ) -> ProviderBatch: ...


@dataclass(frozen=True)
class SyncReport:
    start_date: date
    end_date: date
    codes: tuple[str, ...]
    included_five_minute_bars: bool
    results: tuple[MergeResult, ...]

    def as_dict(self) -> dict[str, object]:
        totals: dict[str, dict[str, int]] = {}
        for result in self.results:
            current = totals.setdefault(
                result.dataset,
                {"incoming": 0, "inserted": 0, "updated": 0, "unchanged": 0, "total": 0},
            )
            current["incoming"] += result.incoming
            current["inserted"] += result.inserted
            current["updated"] += result.updated
            current["unchanged"] += result.unchanged
            current["total"] = result.total
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "codes": list(self.codes),
            "included_five_minute_bars": self.included_five_minute_bars,
            "datasets": totals,
        }


class IngestionPipeline:
    def __init__(self, raw_store: ImmutableRawStore, normalized_store: ParquetDataStore) -> None:
        self.raw_store = raw_store
        self.normalized_store = normalized_store

    def ingest(self, batch: ProviderBatch) -> tuple[list[dict[str, object]], MergeResult]:
        artifact = self.raw_store.persist(batch)
        dataset, rows = normalize_batch(batch, artifact)
        return rows, self.normalized_store.merge(dataset, rows)


class BaoStockSyncService:
    def __init__(self, client: MarketDataClient, pipeline: IngestionPipeline) -> None:
        self.client = client
        self.pipeline = pipeline

    def sync(
        self,
        *,
        start_date: date,
        end_date: date,
        codes: list[str],
        include_five_minute_bars: bool = False,
    ) -> SyncReport:
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        normalized_codes = tuple(dict.fromkeys(code.lower() for code in codes))
        if not normalized_codes:
            raise ValueError("at least one instrument code is required")

        results: list[MergeResult] = []
        with self.client:
            if self.pipeline.normalized_store.read("instruments").num_rows == 0:
                _, result = self.pipeline.ingest(self.client.instruments())
                results.append(result)
            if self.pipeline.normalized_store.read("instruments").num_rows == 0:
                raise DataValidationError("BaoStock returned no instruments")

            stored_calendar = self.pipeline.normalized_store.read(
                "trade_calendar"
            ).to_pylist()
            stored_calendar_dates = {row["calendar_date"] for row in stored_calendar}
            missing_calendar_dates = [
                day
                for day in _date_range(start_date, end_date)
                if day not in stored_calendar_dates
            ]
            if missing_calendar_dates:
                _, result = self.pipeline.ingest(
                    self.client.trade_calendar(
                        start_date=min(missing_calendar_dates),
                        end_date=max(missing_calendar_dates),
                    )
                )
                results.append(result)
            stored_calendar = self.pipeline.normalized_store.read(
                "trade_calendar"
            ).to_pylist()
            stored_calendar_dates = {row["calendar_date"] for row in stored_calendar}
            still_missing_calendar = [
                day
                for day in _date_range(start_date, end_date)
                if day not in stored_calendar_dates
            ]
            if still_missing_calendar:
                missing = ", ".join(day.isoformat() for day in still_missing_calendar[:5])
                raise DataValidationError(f"trade calendar is incomplete: {missing}")
            calendar_rows = [
                row
                for row in stored_calendar
                if start_date <= row["calendar_date"] <= end_date
            ]
            trading_days = [
                row["calendar_date"] for row in calendar_rows if row["is_trading_day"]
            ]

            universe_rows = self.pipeline.normalized_store.read(
                "historical_universe"
            ).to_pylist()
            stored_universe_dates = {row["snapshot_date"] for row in universe_rows}
            for trading_day in trading_days:
                if trading_day in stored_universe_dates:
                    continue
                rows, result = self.pipeline.ingest(
                    self.client.historical_universe(day=trading_day)
                )
                results.append(result)
                if not rows:
                    raise DataValidationError(
                        f"historical universe is empty for {trading_day.isoformat()}"
                    )

            universe_rows = self.pipeline.normalized_store.read(
                "historical_universe"
            ).to_pylist()
            active_pairs = {
                (row["snapshot_date"], row["code"])
                for row in universe_rows
                if row["trade_status"] and row["snapshot_date"] in trading_days
            }
            daily_rows = self.pipeline.normalized_store.read("daily_bars").to_pylist()
            daily_pairs = {
                (row["trade_date"], row["code"])
                for row in daily_rows
                if row["adjustment_flag"] == "3"
            }
            minute_rows = self.pipeline.normalized_store.read(
                "five_minute_bars"
            ).to_pylist()
            minute_pairs = {(row["trade_date"], row["code"]) for row in minute_rows}
            for code in normalized_codes:
                expected_days = [
                    day
                    for day in trading_days
                    if not universe_rows or (day, code) in active_pairs
                ]
                missing_daily = [
                    day for day in expected_days if (day, code) not in daily_pairs
                ]
                if missing_daily:
                    rows, result = self.pipeline.ingest(
                        self.client.daily_bars(
                            code=code,
                            start_date=min(missing_daily),
                            end_date=max(missing_daily),
                        )
                    )
                    results.append(result)
                    fetched_days = {
                        row["trade_date"]
                        for row in rows
                        if row["code"] == code and row["adjustment_flag"] == "3"
                    }
                    still_missing_daily = sorted(set(missing_daily) - fetched_days)
                    if still_missing_daily:
                        missing = ", ".join(
                            day.isoformat() for day in still_missing_daily[:5]
                        )
                        raise DataValidationError(
                            f"daily bars are incomplete for {code}: {missing}"
                        )
                _, result = self.pipeline.ingest(
                    self.client.adjustment_factors(
                        code=code, start_date=start_date, end_date=end_date
                    )
                )
                results.append(result)
                if include_five_minute_bars:
                    missing_minute = [
                        day for day in expected_days if (day, code) not in minute_pairs
                    ]
                    if missing_minute:
                        _, result = self.pipeline.ingest(
                            self.client.five_minute_bars(
                                code=code,
                                start_date=min(missing_minute),
                                end_date=max(missing_minute),
                            )
                        )
                        results.append(result)

        return SyncReport(
            start_date=start_date,
            end_date=end_date,
            codes=normalized_codes,
            included_five_minute_bars=include_five_minute_bars,
            results=tuple(results),
        )


def _date_range(start_date: date, end_date: date) -> list[date]:
    return [
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    ]
