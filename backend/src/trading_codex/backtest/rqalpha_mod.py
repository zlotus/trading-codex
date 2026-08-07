from datetime import datetime
from pathlib import Path

from rqalpha.interface import AbstractMod

from trading_codex.backtest.rqalpha_data_source import RQAlphaParquetDataSource

__config__ = {
    "priority": 0,
    "normalized_root": "data/normalized",
    "as_of": None,
}


class TradingCodexDataMod(AbstractMod):
    def start_up(self, env, mod_config) -> None:
        if not mod_config.as_of:
            raise ValueError("RQAlpha Trading Codex adapter requires an explicit as_of")
        as_of = datetime.fromisoformat(str(mod_config.as_of).replace("Z", "+00:00"))
        env.set_data_source(
            RQAlphaParquetDataSource(
                Path(mod_config.normalized_root),
                as_of=as_of,
            )
        )

    def tear_down(self, code, exception=None) -> None:
        return None


def load_mod() -> TradingCodexDataMod:
    return TradingCodexDataMod()
