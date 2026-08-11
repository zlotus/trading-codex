"""Point-in-time market-data providers, normalization, and quality checks."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "CacheMissError": "trading_codex.data.models",
    "DataValidationError": "trading_codex.data.models",
    "FutureDataError": "trading_codex.data.models",
    "MarketDataError": "trading_codex.data.models",
    "ProviderError": "trading_codex.data.models",
    "RequestBudgetExceeded": "trading_codex.data.models",
    "ParquetDataStore": "trading_codex.data.parquet_store",
    "ParquetDecisionSnapshotSource": "trading_codex.data.decision_source",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
