from functools import lru_cache

from trading_codex.config import get_settings
from trading_codex.ledger.store import SQLiteLedger


@lru_cache
def _get_ledger() -> SQLiteLedger:
    return SQLiteLedger(get_settings().ledger_path)


async def get_ledger() -> SQLiteLedger:
    return _get_ledger()
