import time
from collections.abc import Callable
from datetime import date
from typing import Any

from trading_codex.data.models import CacheMissError, ProviderBatch, RequestBudgetExceeded
from trading_codex.data.raw_store import ImmutableRawStore

MAX_UPSTREAM_REQUESTS_PER_RUN = 1


class CachedBaoStockClient:
    """Read exact BaoStock queries from local raw cache before using the network."""

    def __init__(
        self,
        raw_store: ImmutableRawStore,
        *,
        upstream: Any | None = None,
        allow_network: bool = False,
        max_upstream_requests: int = 1,
        minimum_request_interval: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_upstream_requests < 0:
            raise ValueError("max_upstream_requests must be non-negative")
        if max_upstream_requests > MAX_UPSTREAM_REQUESTS_PER_RUN:
            raise ValueError(
                "max_upstream_requests exceeds the BaoStock safety cap of "
                f"{MAX_UPSTREAM_REQUESTS_PER_RUN}"
            )
        if minimum_request_interval < 0:
            raise ValueError("minimum_request_interval must be non-negative")
        if allow_network and upstream is None:
            raise ValueError("network access requires an explicitly injected test upstream")
        self.raw_store = raw_store
        self.upstream = upstream
        self.allow_network = allow_network
        self.max_upstream_requests = max_upstream_requests
        self.minimum_request_interval = minimum_request_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._upstream_open = False
        self._last_request_at: float | None = None
        self.cache_hits = 0
        self.cache_misses = 0
        self.upstream_requests = 0

    def __enter__(self) -> "CachedBaoStockClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._upstream_open:
            assert self.upstream is not None
            self.upstream.__exit__(exc_type, exc, traceback)
            self._upstream_open = False

    def instruments(self, *, code: str = "") -> ProviderBatch:
        query = {"code": code}
        return self._get(
            operation="instruments",
            query=query,
            loader=lambda: self.upstream.instruments(code=code),
        )

    def trade_calendar(self, *, start_date: date, end_date: date) -> ProviderBatch:
        query = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
        return self._get(
            operation="trade_calendar",
            query=query,
            loader=lambda: self.upstream.trade_calendar(
                start_date=start_date, end_date=end_date
            ),
        )

    def historical_universe(self, *, day: date) -> ProviderBatch:
        query = {"day": day.isoformat()}
        return self._get(
            operation="historical_universe",
            query=query,
            loader=lambda: self.upstream.historical_universe(day=day),
        )

    def daily_bars(
        self,
        *,
        code: str,
        start_date: date,
        end_date: date,
        adjustment_flag: str = "3",
    ) -> ProviderBatch:
        if adjustment_flag not in {"1", "2", "3"}:
            raise ValueError("adjustment_flag must be one of 1, 2, or 3")
        query = {
            "code": code,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "frequency": "d",
            "adjustflag": adjustment_flag,
        }
        return self._get(
            operation="daily_bars",
            query=query,
            loader=lambda: self.upstream.daily_bars(
                code=code,
                start_date=start_date,
                end_date=end_date,
                adjustment_flag=adjustment_flag,
            ),
        )

    def adjustment_factors(
        self, *, code: str, start_date: date, end_date: date
    ) -> ProviderBatch:
        query = {
            "code": code,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        return self._get(
            operation="adjustment_factors",
            query=query,
            loader=lambda: self.upstream.adjustment_factors(
                code=code, start_date=start_date, end_date=end_date
            ),
        )

    def five_minute_bars(
        self, *, code: str, start_date: date, end_date: date
    ) -> ProviderBatch:
        query = {
            "code": code,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "frequency": "5",
            "adjustflag": "3",
        }
        return self._get(
            operation="five_minute_bars",
            query=query,
            loader=lambda: self.upstream.five_minute_bars(
                code=code, start_date=start_date, end_date=end_date
            ),
        )

    def _get(
        self,
        *,
        operation: str,
        query: dict[str, str],
        loader: Callable[[], ProviderBatch],
    ) -> ProviderBatch:
        cached = self.raw_store.lookup(
            source="baostock", operation=operation, query=query
        )
        if cached is not None:
            self.cache_hits += 1
            return cached

        self.cache_misses += 1
        if not self.allow_network:
            raise CacheMissError(
                f"BaoStock cache miss for {operation} ({_query_text(query)}); "
                "network access is disabled"
            )
        if self.upstream_requests >= self.max_upstream_requests:
            raise RequestBudgetExceeded(
                f"BaoStock request budget exhausted before {operation} "
                f"({_query_text(query)})"
            )
        if not self._upstream_open:
            assert self.upstream is not None
            self.upstream.__enter__()
            self._upstream_open = True
        self._throttle()
        self.upstream_requests += 1
        self._last_request_at = self._monotonic()
        batch = loader()
        self.raw_store.persist(batch)
        return batch

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = self._monotonic() - self._last_request_at
        remaining = self.minimum_request_interval - elapsed
        if remaining > 0:
            self._sleep(remaining)


def _query_text(query: dict[str, str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(query.items()))
