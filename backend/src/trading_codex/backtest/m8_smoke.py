import argparse
import hashlib
import json
import math
import os
import resource
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Any

from trading_codex.backtest.evaluation import (
    EvaluationPeriod,
    WalkForwardConfig,
    WalkForwardEvaluator,
)
from trading_codex.backtest.fixed_snapshot import (
    DEFAULT_INDEX_CODES,
    FixedSnapshotEodView,
)
from trading_codex.data.parquet_store import ParquetDataStore
from trading_codex.data.time import SHANGHAI
from trading_codex.domain.contracts import AllocationState
from trading_codex.domain.models import DecisionPoint, PortfolioPosition
from trading_codex.domain.pipeline import DecisionPipeline, DecisionPipelineConfig
from trading_codex.features.momentum import MomentumFeatureConfig
from trading_codex.portfolio.allocation import AllocationConfig
from trading_codex.portfolio.execution import ExecutionConfig
from trading_codex.portfolio.regime_allocation import (
    RegimeAllocationConfig,
    allocation_state,
)
from trading_codex.regime.features import EOD_REGIME_VERSION, MarketRegimeConfig
from trading_codex.risk.engine import RiskConfig
from trading_codex.strategies.pool import StrategyPoolConfig

SMOKE_VERSION = "m8.1-fixed-snapshot-eod-rqalpha-smoke-v1"
PIPELINE_VERSION = "regime-aware-shared-decision-pipeline-m8.1-eod-v1"
ALLOCATION_VERSION = "regime-constrained-allocation-m8.1-eod-v1"
DEFAULT_UNIVERSE_DATE = date(2024, 6, 7)
DEFAULT_HISTORY_CALENDAR_DAYS = 90
DEFAULT_INITIAL_CASH = Decimal("1000000")


@dataclass(frozen=True)
class SmokeParameter:
    parameter_id: str
    max_turnover: Decimal


@dataclass(frozen=True)
class ParameterRun:
    parameter: SmokeParameter
    configuration_id: str
    periods: tuple[EvaluationPeriod, ...]
    observations: tuple[dict[str, Any], ...]
    trade_count: int
    planned_order_count: int
    rqalpha_rejected_order_count: int
    final_total_value: Decimal
    elapsed_seconds: float


DEFAULT_PARAMETERS = (
    SmokeParameter("turnover_10pct", Decimal("0.10")),
    SmokeParameter("turnover_20pct", Decimal("0.20")),
)


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        payload = run_smoke(args)
    except Exception as exc:
        result = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "reason": str(exc),
            "network_access": False,
        }
        sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
        raise SystemExit(2) from exc
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    started = monotonic()
    data_root = args.data_root.expanduser().resolve()
    normalized_root = data_root / "normalized"
    if not normalized_root.is_dir():
        raise FileNotFoundError(f"normalized data root does not exist: {normalized_root}")
    universe_date = args.universe_date
    end_date = args.end_date or _latest_trading_day(normalized_root)
    start_date = args.start_date or universe_date
    if start_date < universe_date:
        raise ValueError("M8.1 fixed-snapshot smoke cannot start before universe_date")
    if end_date < start_date:
        raise ValueError("smoke end_date must not precede start_date")
    material_as_of = args.material_as_of or datetime.now(UTC)
    data_start = start_date - timedelta(days=args.history_calendar_days)

    _progress(
        f"loading bounded fixed universe: {data_start}..{end_date}",
        quiet=args.quiet,
    )
    view = FixedSnapshotEodView(
        normalized_root,
        as_of=material_as_of,
        universe_date=universe_date,
        data_start=data_start,
        data_end=end_date,
        index_codes=tuple(args.index_code),
    )
    trading_days = tuple(
        day for day in view.descriptor.trading_days if start_date <= day <= end_date
    )
    required_periods = args.train_periods + args.test_periods
    if len(trading_days) < required_periods:
        raise ValueError(
            "not enough trading days for one complete walk-forward fold: "
            f"have {len(trading_days)}, require {required_periods}"
        )

    parameter_runs = []
    for index, parameter in enumerate(DEFAULT_PARAMETERS, start=1):
        _progress(
            f"running RQAlpha parameter {index}/{len(DEFAULT_PARAMETERS)}: "
            f"{parameter.parameter_id}",
            quiet=args.quiet,
        )
        parameter_runs.append(
            _run_parameter(
                view,
                trading_days=trading_days,
                parameter=parameter,
                initial_cash=args.initial_cash,
                progress_every=args.progress_every,
                quiet=args.quiet,
            )
        )

    evaluation_config = WalkForwardConfig(
        train_periods=args.train_periods,
        test_periods=args.test_periods,
        bootstrap_samples=args.bootstrap_samples,
    )
    report = WalkForwardEvaluator(evaluation_config).evaluate(
        {run.parameter.parameter_id: run.periods for run in parameter_runs}
    )
    generated_at = datetime.now(UTC)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    body = {
        "version": SMOKE_VERSION,
        "status": "passed",
        "generated_at": generated_at,
        "network_access": False,
        "research_boundary": {
            "fixed_snapshot_universe": True,
            "survivorship_bias": True,
            "formal_m4_oos": False,
            "point_in_time_universe": False,
            "official_index_benchmark": False,
            "corporate_actions_applied": False,
            "opening_0935_feature": False,
            "purpose": "engineering_smoke_only",
        },
        "data": {
            "normalized_root": str(normalized_root),
            "material_as_of": view.as_of,
            "universe_date": universe_date,
            "index_codes": view.descriptor.index_codes,
            "universe_size": len(view.descriptor.codes),
            "data_start": view.descriptor.data_start,
            "evaluation_start": trading_days[0],
            "evaluation_end": trading_days[-1],
            "trading_days": len(trading_days),
            "daily_rows_by_adjustment": view.descriptor.daily_rows_by_adjustment,
            "source_payload_count": len(view.descriptor.source_payloads),
            "source_payloads": view.descriptor.source_payloads,
            "source_payloads_sha256": view.descriptor.source_payloads_sha256,
            "load_seconds": view.descriptor.load_seconds,
        },
        "execution": {
            "adapter": "RQAlphaParquetDataSource",
            "rqalpha_version": _rqalpha_version(),
            "frequency": "1d",
            "matching_type": "current_bar",
            "initial_cash": args.initial_cash,
            "cost_model": asdict(ExecutionConfig()),
        },
        "walk_forward": {
            "config": asdict(evaluation_config),
            "report": asdict(report),
            "benchmark": {
                "id": "fixed_snapshot_universe_equal_weight_unadjusted_v1",
                "description": (
                    "每日对当日已有行情的固定快照成分等权；停牌记零收益；非官方指数"
                ),
            },
        },
        "parameters": [_parameter_payload(run) for run in parameter_runs],
        "code": _code_descriptor(),
        "resources": {
            "elapsed_seconds": monotonic() - started,
            "user_cpu_seconds": usage.ru_utime,
            "system_cpu_seconds": usage.ru_stime,
            "peak_rss_kib": usage.ru_maxrss,
        },
    }
    artifact_bytes = _json_bytes(body)
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_root = args.artifact_root or data_root / "artifacts" / "m8.1"
    artifact_path = _write_immutable(
        artifact_root,
        f"m8-smoke-{artifact_sha256}.json",
        artifact_bytes,
    )
    return {
        "status": "passed",
        "network_access": False,
        "artifact": str(artifact_path),
        "artifact_sha256": artifact_sha256,
        "survivorship_bias": True,
        "formal_m4_oos": False,
        "universe_size": len(view.descriptor.codes),
        "trading_days": len(trading_days),
        "folds": len(report.folds),
        "out_of_sample": _json_value(asdict(report.out_of_sample)),
        "elapsed_seconds": body["resources"]["elapsed_seconds"],
        "peak_rss_kib": body["resources"]["peak_rss_kib"],
    }


def _run_parameter(
    view: FixedSnapshotEodView,
    *,
    trading_days: tuple[date, ...],
    parameter: SmokeParameter,
    initial_cash: Decimal,
    progress_every: int,
    quiet: bool,
) -> ParameterRun:
    try:
        from rqalpha import run_func
        from rqalpha.api import order_shares, update_universe
    except ImportError as exc:
        raise RuntimeError(
            "M8.1 smoke requires the isolated RQAlpha 6.3.0 environment; "
            "see spikes/rqalpha/README.md"
        ) from exc

    started = monotonic()
    pipeline = DecisionPipeline(_pipeline_config(parameter))
    previous_allocation: AllocationState | None = None
    regimes: dict[date, Any] = {}
    decision_ids: dict[date, str] = {}
    planned_orders: dict[date, int] = {}
    rejected_orders: dict[date, int] = {}
    day_set = set(trading_days)
    order_book_ids = [_baostock_to_order_book_id(code) for code in view.codes]

    def init(context: Any) -> None:
        del context
        update_universe(order_book_ids)

    def handle_bar(context: Any, bar_dict: Any) -> None:
        del bar_dict
        nonlocal previous_allocation
        current = context.now.date()
        if current not in day_set:
            return
        snapshot = view.snapshot(
            decision_date=current,
            as_of=_eod(current),
            cash=_decimal(context.portfolio.cash),
            positions=_rqalpha_positions(context),
            priced_observations=21,
        )
        run = pipeline.run(snapshot, previous_allocation=previous_allocation)
        previous_allocation = allocation_state(snapshot.as_of, run.allocated)
        regimes[current] = run.regime.selected
        decision_ids[current] = run.decision_id
        planned_orders[current] = len(run.execution.orders)
        rejected = 0
        for order in run.execution.orders:
            quantity = order.quantity if order.side.value == "buy" else -order.quantity
            result = order_shares(_baostock_to_order_book_id(order.code), quantity)
            if result is None or getattr(getattr(result, "status", None), "name", "") == "REJECTED":
                rejected += 1
        rejected_orders[current] = rejected
        completed = len(regimes)
        if progress_every and completed % progress_every == 0:
            _progress(
                f"{parameter.parameter_id}: {completed}/{len(trading_days)} {current}",
                quiet=quiet,
            )

    result = run_func(
        config=_rqalpha_config(
            view,
            start_date=trading_days[0],
            end_date=trading_days[-1],
            initial_cash=initial_cash,
        ),
        init=init,
        handle_bar=handle_bar,
    )
    analyser = result.get("sys_analyser")
    if not analyser:
        raise RuntimeError("RQAlpha sys_analyser did not return a result")
    portfolio = analyser["portfolio"]
    trades = analyser["trades"]
    periods, observations, final_value = _evaluation_periods(
        portfolio,
        trades,
        trading_days=trading_days,
        regimes=regimes,
        decision_ids=decision_ids,
        view=view,
        initial_cash=initial_cash,
    )
    return ParameterRun(
        parameter=parameter,
        configuration_id=pipeline.configuration_id,
        periods=periods,
        observations=observations,
        trade_count=len(trades),
        planned_order_count=sum(planned_orders.values()),
        rqalpha_rejected_order_count=sum(rejected_orders.values()),
        final_total_value=final_value,
        elapsed_seconds=monotonic() - started,
    )


def _evaluation_periods(
    portfolio: Any,
    trades: Any,
    *,
    trading_days: tuple[date, ...],
    regimes: dict[date, Any],
    decision_ids: dict[date, str],
    view: FixedSnapshotEodView,
    initial_cash: Decimal,
) -> tuple[tuple[EvaluationPeriod, ...], tuple[dict[str, Any], ...], Decimal]:
    rows = {_as_date(index): row for index, row in portfolio.iterrows()}
    costs = _transaction_costs_by_date(trades)
    periods = []
    observations = []
    previous_value = initial_cash
    for day in trading_days:
        if day not in rows or day not in regimes or day not in decision_ids:
            raise RuntimeError(f"RQAlpha output is missing the decision grid on {day}")
        row = rows[day]
        total_value = _decimal(row["total_value"])
        if "daily_returns" in row:
            net_return = _finite_decimal(row["daily_returns"], default=Decimal(0))
        else:
            net_return = total_value / previous_value - Decimal(1)
        transaction_cost = costs.get(day, Decimal(0))
        cost_rate = transaction_cost / previous_value
        gross_return = net_return + cost_rate
        benchmark_return = view.benchmark_return(day)
        period = EvaluationPeriod(
            as_of=_eod(day),
            gross_return=gross_return,
            benchmark_return=benchmark_return,
            cost_rate=cost_rate,
            regime=regimes[day],
        )
        periods.append(period)
        observations.append(
            {
                "as_of": period.as_of,
                "decision_id": decision_ids[day],
                "gross_return": period.gross_return,
                "net_return": period.net_return,
                "benchmark_return": period.benchmark_return,
                "cost_rate": period.cost_rate,
                "transaction_cost": transaction_cost,
                "total_value": total_value,
                "regime": period.regime,
            }
        )
        previous_value = total_value
    return tuple(periods), tuple(observations), previous_value


def _pipeline_config(parameter: SmokeParameter) -> DecisionPipelineConfig:
    allocation = RegimeAllocationConfig(
        base=AllocationConfig(),
        max_turnover=parameter.max_turnover,
        strategy_change_points=(DecisionPoint.EOD,),
        version=ALLOCATION_VERSION,
    )
    return DecisionPipelineConfig(
        features=MomentumFeatureConfig(),
        regime=MarketRegimeConfig(
            opening_feature_enabled=False,
            version=EOD_REGIME_VERSION,
        ),
        strategies=StrategyPoolConfig(),
        allocation=allocation,
        risk=RiskConfig(),
        execution=ExecutionConfig(),
        version=PIPELINE_VERSION,
    )


def _rqalpha_config(
    view: FixedSnapshotEodView,
    *,
    start_date: date,
    end_date: date,
    initial_cash: Decimal,
) -> dict[str, Any]:
    return {
        "base": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "frequency": "1d",
            "accounts": {"stock": float(initial_cash)},
            "data_bundle_path": "/tmp/trading-codex-unused-rqalpha-bundle",
            "capital_gain_tax_rate": 0,
        },
        "extra": {"log_level": "error"},
        "mod": {
            "trading_codex_data": {
                "enabled": True,
                "lib": "trading_codex.backtest.rqalpha_mod",
                "priority": 0,
                "normalized_root": str(view.normalized_root),
                "as_of": view.as_of.isoformat(),
                "codes": list(view.codes),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
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


def _parameter_payload(run: ParameterRun) -> dict[str, Any]:
    return {
        "parameter_id": run.parameter.parameter_id,
        "configuration_id": run.configuration_id,
        "config": asdict(_pipeline_config(run.parameter)),
        "trade_count": run.trade_count,
        "planned_order_count": run.planned_order_count,
        "rqalpha_rejected_order_count": run.rqalpha_rejected_order_count,
        "final_total_value": run.final_total_value,
        "elapsed_seconds": run.elapsed_seconds,
        "observations": run.observations,
    }


def _latest_trading_day(normalized_root: Path) -> date:
    now = datetime.now(UTC)
    table = ParquetDataStore(normalized_root).scan(
        "trade_calendar",
        as_of=now,
        columns=("calendar_date", "is_trading_day"),
        ranges={"calendar_date": (None, now.astimezone(SHANGHAI).date())},
    )
    days = [
        row["calendar_date"]
        for row in table.to_pylist()
        if row["is_trading_day"]
    ]
    if not days:
        raise ValueError("normalized trade calendar has no completed trading day")
    return max(days)


def _rqalpha_positions(context: Any) -> tuple[PortfolioPosition, ...]:
    positions = []
    for order_book_id, position in context.portfolio.positions.items():
        quantity = int(position.quantity)
        if quantity <= 0:
            continue
        positions.append(
            PortfolioPosition(
                code=_order_book_id_to_baostock(str(order_book_id)),
                quantity=quantity,
                sellable_quantity=int(position.sellable),
                average_cost=_decimal(position.avg_price),
            )
        )
    return tuple(sorted(positions, key=lambda item: item.code))


def _transaction_costs_by_date(trades: Any) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    if trades is None or len(trades) == 0:
        return result
    for index, row in trades.iterrows():
        timestamp = next(
            (
                row[name]
                for name in ("datetime", "trading_datetime", "calendar_datetime")
                if name in row and row[name] is not None
            ),
            index,
        )
        day = _as_date(timestamp)
        result[day] = result.get(day, Decimal(0)) + _decimal(row["transaction_cost"])
    return result


def _code_descriptor() -> dict[str, Any]:
    root = _repository_root()
    try:
        commit = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-c", f"safe.directory={root}", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
        dirty = True
    digest = hashlib.sha256()
    source_root = root / "backend" / "src" / "trading_codex"
    paths = [
        *source_root.rglob("*.py"),
        root / "pyproject.toml",
        root / "spikes" / "rqalpha" / "requirements.txt",
    ]
    hashed_files = 0
    for path in sorted(value for value in paths if value.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        hashed_files += 1
    if hashed_files == 0:
        raise RuntimeError(f"cannot locate Python sources below repository root: {root}")
    return {
        "git_commit": commit,
        "git_dirty": dirty,
        "python_source_sha256": digest.hexdigest(),
        "hashed_files": hashed_files,
    }


def _repository_root() -> Path:
    candidates = (Path.cwd().resolve(), *Path(__file__).resolve().parents)
    for candidate in candidates:
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "backend" / "src" / "trading_codex").is_dir()
        ):
            return candidate
    raise RuntimeError("cannot locate the trading-codex repository root")


def _write_immutable(root: Path, filename: str, payload: bytes) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target = root / filename
    if target.exists():
        if target.read_bytes() != payload:
            raise RuntimeError(f"immutable artifact collision: {target}")
        return target
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise RuntimeError(f"immutable artifact collision: {target}")
        return target
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _json_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("artifact floats must be finite")
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported artifact value: {type(value).__name__}")


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _finite_decimal(value: Any, *, default: Decimal) -> Decimal:
    numeric = _decimal(value)
    return numeric if numeric.is_finite() else default


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        return value.date()
    return datetime.fromisoformat(str(value)).date()


def _eod(day: date) -> datetime:
    return datetime.combine(day, time(15), tzinfo=SHANGHAI).astimezone(UTC)


def _baostock_to_order_book_id(code: str) -> str:
    exchange, number = code.split(".", maxsplit=1)
    suffix = {"sh": "XSHG", "sz": "XSHE", "bj": "XBEI"}.get(exchange)
    if suffix is None:
        raise ValueError(f"unsupported BaoStock exchange: {exchange}")
    return f"{number}.{suffix}"


def _order_book_id_to_baostock(order_book_id: str) -> str:
    number, exchange = order_book_id.split(".", maxsplit=1)
    prefix = {"XSHG": "sh", "XSHE": "sz", "XBEI": "bj"}.get(exchange)
    if prefix is None:
        raise ValueError(f"unsupported RQAlpha exchange: {exchange}")
    return f"{prefix}.{number}"


def _rqalpha_version() -> str:
    try:
        import rqalpha
    except ImportError as exc:
        raise RuntimeError("RQAlpha is unavailable in the current environment") from exc
    return str(rqalpha.__version__)


def _progress(message: str, *, quiet: bool) -> None:
    if not quiet:
        sys.stderr.write(f"[M8.1] {message}\n")
        sys.stderr.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-codex-m8-smoke",
        description="Run the survivorship-biased fixed-universe M8.1 EOD smoke.",
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--universe-date", type=date.fromisoformat, default=DEFAULT_UNIVERSE_DATE)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--material-as-of", type=_aware_datetime)
    parser.add_argument(
        "--index-code",
        action="append",
        default=list(DEFAULT_INDEX_CODES),
        help="fixed snapshot index code; repeat for multiple indexes",
    )
    parser.add_argument(
        "--history-calendar-days",
        type=int,
        default=DEFAULT_HISTORY_CALENDAR_DAYS,
    )
    parser.add_argument("--initial-cash", type=Decimal, default=DEFAULT_INITIAL_CASH)
    parser.add_argument("--train-periods", type=int, default=252)
    parser.add_argument("--test-periods", type=int, default=63)
    parser.add_argument("--bootstrap-samples", type=int, default=1_000)
    parser.add_argument("--progress-every", type=int, default=20)
    parser.add_argument("--quiet", action="store_true")
    return parser


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("material-as-of must include a UTC offset")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    main()
