from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Context, localcontext

from trading_codex.domain.contracts import DecisionRun
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import DecisionSnapshot
from trading_codex.features.momentum import (
    MomentumFeatureConfig,
    MomentumFeaturePipeline,
)
from trading_codex.portfolio.allocation import AllocationConfig, TargetAllocator
from trading_codex.portfolio.execution import ExecutionConfig, ExecutionPlanner
from trading_codex.risk.engine import HardRiskEngine, RiskConfig
from trading_codex.strategies.momentum import VolatilityScaledMomentumStrategy

PIPELINE_VERSION = "shared-decision-pipeline-v1"


@dataclass(frozen=True)
class DecisionPipelineConfig:
    features: MomentumFeatureConfig = field(default_factory=MomentumFeatureConfig)
    allocation: AllocationConfig = field(default_factory=AllocationConfig)
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
                "strategy_version": VolatilityScaledMomentumStrategy.version,
            }
        )


class DecisionPipeline:
    def __init__(self, config: DecisionPipelineConfig | None = None) -> None:
        self.config = config or DecisionPipelineConfig()
        self.feature_pipeline = MomentumFeaturePipeline(self.config.features)
        self.strategy = VolatilityScaledMomentumStrategy()
        self.allocator = TargetAllocator(self.config.allocation)
        self.risk_engine = HardRiskEngine(self.config.risk)
        self.execution_planner = ExecutionPlanner(self.config.execution)

    @property
    def configuration_id(self) -> str:
        return self.config.configuration_id

    def run(self, snapshot: DecisionSnapshot) -> DecisionRun:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            self.risk_engine.validate_snapshot(snapshot)
            features = self.feature_pipeline.compute(snapshot)
            proposal = self.strategy.propose(features)
            allocated = self.allocator.allocate(proposal)
            risk = self.risk_engine.evaluate(snapshot, allocated)
            execution = self.execution_planner.plan(snapshot, risk)
            decision_id = canonical_sha256(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "configuration_id": self.configuration_id,
                    "features": features,
                    "proposal": proposal,
                    "allocated": allocated,
                    "risk": risk,
                    "execution": execution,
                }
            )
            return DecisionRun(
                decision_id=decision_id,
                snapshot_id=snapshot.snapshot_id,
                configuration_id=self.configuration_id,
                pipeline_version=self.config.version,
                features=features,
                proposal=proposal,
                allocated=allocated,
                risk=risk,
                execution=execution,
            )
