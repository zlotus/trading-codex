"""Execution-backtest adapters and framework compatibility boundaries."""

from trading_codex.backtest.evaluation import (
    EvaluationPeriod,
    WalkForwardEvaluator,
    WalkForwardReport,
)
from trading_codex.backtest.replay import HistoricalReplay

__all__ = [
    "EvaluationPeriod",
    "HistoricalReplay",
    "WalkForwardEvaluator",
    "WalkForwardReport",
]
