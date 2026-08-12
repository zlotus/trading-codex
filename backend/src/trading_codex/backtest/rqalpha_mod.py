from datetime import datetime
from pathlib import Path

from rqalpha.interface import AbstractMod

from trading_codex.backtest.rqalpha_data_source import RQAlphaParquetDataSource

__config__ = {
    "priority": 0,
    "normalized_root": "data/normalized",
    "as_of": None,
    "codes": (),
    "start_date": None,
    "end_date": None,
}


class TradingCodexDataMod(AbstractMod):
    def start_up(self, env, mod_config) -> None:
        if not mod_config.as_of:
            raise ValueError("RQAlpha Trading Codex adapter requires an explicit as_of")
        as_of = datetime.fromisoformat(str(mod_config.as_of).replace("Z", "+00:00"))
        codes = tuple(str(value) for value in (mod_config.codes or ()))
        start_date = (
            datetime.fromisoformat(str(mod_config.start_date)).date()
            if mod_config.start_date
            else None
        )
        end_date = (
            datetime.fromisoformat(str(mod_config.end_date)).date()
            if mod_config.end_date
            else None
        )
        env.set_data_source(
            RQAlphaParquetDataSource(
                Path(mod_config.normalized_root),
                as_of=as_of,
                codes=codes,
                start_date=start_date,
                end_date=end_date,
            )
        )

    def tear_down(self, code, exception=None) -> None:
        return None


def load_mod() -> TradingCodexDataMod:
    return TradingCodexDataMod()
