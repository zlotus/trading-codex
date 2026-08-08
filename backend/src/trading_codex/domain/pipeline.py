from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Context, localcontext

from trading_codex.domain.contracts import AllocationState, DecisionRun
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import DecisionSnapshot
from trading_codex.features.momentum import (
    MomentumFeatureConfig,
    MomentumFeaturePipeline,
)
from trading_codex.portfolio.execution import ExecutionConfig, ExecutionPlanner
from trading_codex.portfolio.regime_allocation import (
    RegimeAllocationConfig,
    RegimeAwareAllocator,
)
from trading_codex.regime.features import MarketRegimeConfig, MarketRegimeFeaturePipeline
from trading_codex.risk.engine import HardRiskEngine, RiskConfig
from trading_codex.strategies.pool import StrategyPool, StrategyPoolConfig

PIPELINE_VERSION = "regime-aware-shared-decision-pipeline-v2"


@dataclass(frozen=True)
class DecisionPipelineConfig:
    features: MomentumFeatureConfig = field(default_factory=MomentumFeatureConfig)
    regime: MarketRegimeConfig = field(default_factory=MarketRegimeConfig)
    strategies: StrategyPoolConfig = field(default_factory=StrategyPoolConfig)
    allocation: RegimeAllocationConfig = field(default_factory=RegimeAllocationConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    version: str = PIPELINE_VERSION

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("pipeline version is required")

    @property
    def configuration_id(self) -> str:
        return canonical_sha256(
            {
                "pipeline": self,
                "strategy_versions": StrategyPool(self.strategies).versions,
            }
        )


class DecisionPipeline:
    def __init__(self, config: DecisionPipelineConfig | None = None) -> None:
        self.config = config or DecisionPipelineConfig()
        self.feature_pipeline = MomentumFeaturePipeline(self.config.features)
        self.regime_pipeline = MarketRegimeFeaturePipeline(self.config.regime)
        self.strategy_pool = StrategyPool(self.config.strategies)
        self.allocator = RegimeAwareAllocator(self.config.allocation)
        self.risk_engine = HardRiskEngine(self.config.risk)
        self.execution_planner = ExecutionPlanner(self.config.execution)

    @property
    def configuration_id(self) -> str:
        return self.config.configuration_id

    def run(
        self,
        snapshot: DecisionSnapshot,
        *,
        previous_allocation: AllocationState | None = None,
    ) -> DecisionRun:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            self.risk_engine.validate_snapshot(snapshot)
            features = self.feature_pipeline.compute(snapshot)
            regime = self.regime_pipeline.compute(snapshot)
            proposals = self.strategy_pool.propose(features)
            allocated = self.allocator.allocate(
                snapshot,
                regime,
                proposals,
                previous=previous_allocation,
            )
            proposal = next(
                item for item in proposals if item.strategy is allocated.active_strategy
            )
            risk = self.risk_engine.evaluate(snapshot, allocated)
            execution = self.execution_planner.plan(snapshot, risk)
            decision_id = canonical_sha256(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "configuration_id": self.configuration_id,
                    "features": features,
                    "regime": regime,
                    "strategy_proposals": proposals,
                    "proposal": proposal,
                    "allocated": allocated,
                    "risk": risk,
                    "execution": execution,
                    "previous_allocation": previous_allocation,
                    "allocator_version": self.allocator.version,
                }
            )
            return DecisionRun(
                decision_id=decision_id,
                snapshot_id=snapshot.snapshot_id,
                configuration_id=self.configuration_id,
                pipeline_version=self.config.version,
                features=features,
                regime=regime,
                strategy_proposals=proposals,
                proposal=proposal,
                allocated=allocated,
                risk=risk,
                execution=execution,
                previous_allocation=previous_allocation,
                allocator_version=self.allocator.version,
            )
