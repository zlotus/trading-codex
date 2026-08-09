from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import TYPE_CHECKING

from trading_codex.ai.contracts import (
    AIClientResult,
    AICompletionOutcome,
    AIOverlayEvaluation,
    AIProposalDraft,
    AIProposalStatus,
    AIRequestContext,
    AvailableEvidence,
)
from trading_codex.domain.contracts import (
    DecisionRun,
    StrategyAllocation,
    StrategyKind,
    TargetPortfolio,
    TargetWeight,
)
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import (
    DecisionKernelError,
    DecisionSnapshot,
    SnapshotValidationError,
)
from trading_codex.portfolio.allocation import WEIGHT_QUANTUM, AllocationConfig, TargetAllocator
from trading_codex.portfolio.execution import ExecutionConfig, ExecutionPlanner
from trading_codex.risk.engine import HardRiskEngine, RiskConfig

if TYPE_CHECKING:
    from trading_codex.domain.pipeline import DecisionPipeline

AI_OVERLAY_VERSION = "bounded-ai-shadow-allocation-v1"


@dataclass(frozen=True)
class AIOverlayConfig:
    base_configuration_id: str
    allocation: AllocationConfig
    risk: RiskConfig
    execution: ExecutionConfig
    max_strategy_weight_change: Decimal = Decimal("0.20")
    max_overlay_turnover: Decimal = Decimal("0.10")
    version: str = AI_OVERLAY_VERSION

    def __post_init__(self) -> None:
        if not self.base_configuration_id:
            raise ValueError("AI overlay base configuration id is required")
        if not Decimal(0) <= self.max_strategy_weight_change <= Decimal(1):
            raise ValueError("AI strategy weight change must be in [0, 1]")
        if not Decimal(0) <= self.max_overlay_turnover <= Decimal(1):
            raise ValueError("AI overlay turnover must be in [0, 1]")
        if not self.version:
            raise ValueError("AI overlay version is required")


@dataclass(frozen=True)
class AIShadowResult:
    context: AIRequestContext
    client: AIClientResult
    evaluation: AIOverlayEvaluation
    decision: DecisionRun


class BoundedAIOverlay:
    def __init__(self, config: AIOverlayConfig) -> None:
        self.config = config
        self.target_allocator = TargetAllocator(self.config.allocation)
        self.risk_engine = HardRiskEngine(self.config.risk)
        self.execution_planner = ExecutionPlanner(self.config.execution)

    @property
    def version(self) -> str:
        return self.config.version

    @classmethod
    def from_pipeline(cls, pipeline: DecisionPipeline) -> BoundedAIOverlay:
        return cls(
            AIOverlayConfig(
                base_configuration_id=pipeline.configuration_id,
                allocation=pipeline.config.allocation.base,
                risk=pipeline.config.risk,
                execution=pipeline.config.execution,
            )
        )

    def evaluate(
        self,
        snapshot: DecisionSnapshot,
        base: DecisionRun,
        context: AIRequestContext,
        client: AIClientResult,
    ) -> AIOverlayEvaluation:
        self._validate_base(snapshot, base, context)
        if client.outcome is not AICompletionOutcome.SUCCEEDED:
            return self._fallback(
                base,
                context,
                client,
                status=AIProposalStatus.FALLBACK,
                errors=(f"{client.outcome.value}: {client.error}",),
            )

        assert client.draft is not None
        errors = self._validate_draft(context, client, client.draft)
        target: TargetPortfolio | None = None
        if not errors:
            try:
                target = self._target(base, client.draft)
            except SnapshotValidationError as error:
                errors.append(str(error))
        if target is not None:
            base_gross = _gross(base.allocated.weights)
            if _gross(target.weights) > base_gross:
                errors.append("AI target cannot increase gross exposure above the base target")
            if target.turnover > self.config.max_overlay_turnover:
                errors.append("AI target exceeds the overlay turnover limit")
        if errors or target is None:
            return self._fallback(
                base,
                context,
                client,
                status=AIProposalStatus.REJECTED,
                errors=tuple(errors),
                draft=client.draft,
            )

        try:
            risk = self.risk_engine.evaluate(snapshot, target)
            execution = self.execution_planner.plan(snapshot, risk)
        except DecisionKernelError as error:
            return self._fallback(
                base,
                context,
                client,
                status=AIProposalStatus.REJECTED,
                errors=(f"deterministic risk/execution rejected AI target: {error}",),
                draft=client.draft,
            )
        normalized = _normalized_allocations(client.draft)
        proposal_id = _proposal_id(
            context=context,
            draft=client.draft,
            status=AIProposalStatus.ACCEPTED,
            errors=(),
            version=self.version,
        )
        return AIOverlayEvaluation(
            proposal_id=proposal_id,
            status=AIProposalStatus.ACCEPTED,
            summary=client.draft.summary,
            rationale=client.draft.rationale,
            strategy_weights=normalized,
            risk_scale=client.draft.risk_scale,
            evidence=client.draft.evidence,
            validation_errors=(),
            assistant_message=client.draft.assistant_message,
            target=target,
            risk=risk,
            execution=execution,
        )

    def _validate_base(
        self,
        snapshot: DecisionSnapshot,
        base: DecisionRun,
        context: AIRequestContext,
    ) -> None:
        if base.snapshot_id != snapshot.snapshot_id or context.snapshot_id != snapshot.snapshot_id:
            raise SnapshotValidationError("AI overlay uses a different snapshot")
        if context.base_decision_id != base.decision_id:
            raise SnapshotValidationError("AI context uses a different base decision")
        if base.configuration_id != self.config.base_configuration_id:
            raise SnapshotValidationError(
                "AI overlay configuration differs from the base decision pipeline"
            )
        if (
            context.as_of != snapshot.as_of
            or context.decision_deadline != snapshot.execution_deadline
        ):
            raise SnapshotValidationError("AI context uses a different decision boundary")
        if context.base_target_weights != base.allocated.weights:
            raise SnapshotValidationError("AI context base targets are inconsistent")
        expected_gross_limit = min(
            _gross(base.allocated.weights),
            self.config.allocation.max_gross_exposure,
        )
        if (
            context.max_strategy_weight_change
            != self.config.max_strategy_weight_change
            or context.max_overlay_turnover != self.config.max_overlay_turnover
            or context.max_target_gross_exposure != expected_gross_limit
        ):
            raise SnapshotValidationError("AI context overlay limits are inconsistent")

    def _validate_draft(
        self,
        context: AIRequestContext,
        client: AIClientResult,
        draft: AIProposalDraft,
    ) -> list[str]:
        errors: list[str] = []
        if draft.decision_id != context.base_decision_id:
            errors.append("AI proposal references a different decision")
        if draft.snapshot_id != context.snapshot_id:
            errors.append("AI proposal references a different snapshot")
        if client.requested_at < context.as_of:
            errors.append("AI request precedes the decision as_of")
        if draft.generated_at < context.as_of or draft.generated_at > client.completed_at:
            errors.append("AI proposal generated_at is outside the observed request window")
        if not client.cache_hit and draft.generated_at < client.requested_at:
            errors.append("AI proposal generated_at precedes the provider request")
        if client.completed_at >= context.decision_deadline:
            errors.append("AI proposal arrived after the decision deadline")
        if draft.valid_until > context.decision_deadline:
            errors.append("AI proposal validity exceeds the decision deadline")
        if client.completed_at >= draft.valid_until:
            errors.append("AI proposal arrived after its validity boundary")

        approved = set(context.approved_strategies)
        parsed: dict[StrategyKind, Decimal] = {}
        for item in draft.strategy_weights:
            try:
                strategy = StrategyKind(item.strategy)
            except ValueError:
                errors.append(f"AI proposal references unknown strategy {item.strategy!r}")
                continue
            if strategy not in approved:
                errors.append(f"AI proposal references unapproved strategy {strategy.value!r}")
                continue
            parsed[strategy] = item.weight
        if len(parsed) == len(draft.strategy_weights):
            if sum(parsed.values(), Decimal(0)) != Decimal(1):
                errors.append("AI strategy weights must sum to one")
            base_weights = {item.strategy: item.weight for item in context.base_strategy_weights}
            for strategy in approved:
                change = abs(
                    parsed.get(strategy, Decimal(0))
                    - base_weights.get(strategy, Decimal(0))
                )
                if change > self.config.max_strategy_weight_change:
                    errors.append(
                        f"AI strategy weight change exceeds limit for {strategy.value}"
                    )

        known_evidence = {item.evidence_id for item in context.evidence}
        for citation in draft.evidence:
            if citation.evidence_id not in known_evidence:
                errors.append(
                    f"AI proposal cites unknown evidence {citation.evidence_id!r}"
                )
        return errors

    def _target(self, base: DecisionRun, draft: AIProposalDraft) -> TargetPortfolio:
        allocations = _normalized_allocations(draft)
        base_allocations = base.allocated.strategy_allocations or (
            StrategyAllocation(strategy=base.allocated.active_strategy, weight=Decimal(1)),
        )
        if allocations == base_allocations and draft.risk_scale == Decimal(1):
            return replace(base.allocated, version=self.version, turnover=Decimal(0))

        proposals = {item.strategy: item for item in base.strategy_proposals}
        component_weights: dict[str, Decimal] = {}
        component_ranks: dict[str, int] = {}
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            for allocation in allocations:
                if allocation.strategy is StrategyKind.CASH:
                    continue
                if allocation.strategy is base.allocated.active_strategy:
                    component = base.allocated.weights
                else:
                    component = self.target_allocator.allocate(
                        proposals[allocation.strategy]
                    ).weights
                for item in component:
                    contribution = item.weight * allocation.weight * draft.risk_scale
                    component_weights[item.code] = component_weights.get(
                        item.code,
                        Decimal(0),
                    ) + contribution
                    component_ranks[item.code] = min(
                        component_ranks.get(item.code, item.rank),
                        item.rank,
                    )

            selected = sorted(
                component_weights,
                key=lambda code: (-component_weights[code], code),
            )[: self.config.allocation.max_positions]
            weights = tuple(
                sorted(
                    (
                        TargetWeight(
                            code=code,
                            weight=min(
                                component_weights[code],
                                self.config.allocation.max_position_weight,
                            ).quantize(WEIGHT_QUANTUM, rounding=ROUND_DOWN),
                            rank=component_ranks[code],
                        )
                        for code in selected
                        if component_weights[code] >= WEIGHT_QUANTUM
                    ),
                    key=lambda item: item.code,
                )
            )
            gross = _gross(weights)
            if gross > self.config.allocation.max_gross_exposure:
                raise SnapshotValidationError("AI target exceeds allocation gross exposure")
            cash_weight = Decimal(1) - gross
            turnover = _target_turnover(base.allocated, weights, cash_weight)
        return TargetPortfolio(
            snapshot_id=base.snapshot_id,
            version=self.version,
            weights=weights,
            cash_weight=cash_weight,
            active_strategy=base.allocated.active_strategy,
            strategy_allocations=allocations,
            turnover=turnover,
            emergency_risk_off=base.allocated.emergency_risk_off,
            decision_point=base.allocated.decision_point,
        )

    def _fallback(
        self,
        base: DecisionRun,
        context: AIRequestContext,
        client: AIClientResult,
        *,
        status: AIProposalStatus,
        errors: tuple[str, ...],
        draft: AIProposalDraft | None = None,
    ) -> AIOverlayEvaluation:
        allocations = base.allocated.strategy_allocations or (
            StrategyAllocation(strategy=base.allocated.active_strategy, weight=Decimal(1)),
        )
        evidence = draft.evidence if draft is not None else ()
        summary = draft.summary if draft is not None else "AI 未产生可用提案"
        rationale = (
            draft.rationale
            if draft is not None
            else "确定性 fallback 保持基础组合不变。"
        )
        message = "本次未应用 AI 调整，AI-shadow 保持基础组合不变。"
        proposal_id = _proposal_id(
            context=context,
            draft=draft,
            status=status,
            errors=errors,
            version=self.version,
        )
        target = replace(base.allocated, version=self.version, turnover=Decimal(0))
        risk = replace(base.risk, requested=target)
        return AIOverlayEvaluation(
            proposal_id=proposal_id,
            status=status,
            summary=summary,
            rationale=rationale,
            strategy_weights=allocations,
            risk_scale=Decimal(1),
            evidence=evidence,
            validation_errors=errors,
            assistant_message=message,
            target=target,
            risk=risk,
            execution=base.execution,
        )


def build_ai_context(
    snapshot: DecisionSnapshot,
    base: DecisionRun,
    *,
    overlay_config: AIOverlayConfig,
) -> AIRequestContext:
    if base.snapshot_id != snapshot.snapshot_id:
        raise SnapshotValidationError("AI context base decision uses a different snapshot")
    if not base.allocated.strategy_allocations:
        raise SnapshotValidationError(
            "AI context requires an explicit base strategy allocation"
        )
    allocations = base.allocated.strategy_allocations
    features = base.regime.features
    evidence = (
        AvailableEvidence(
            evidence_id="regime.selected",
            label="Selected market regime",
            value=base.regime.selected.value,
        ),
        *(
            AvailableEvidence(
                evidence_id=f"regime.probability.{item.label.value}",
                label=f"Probability of {item.label.value}",
                value=format(item.probability, "f"),
            )
            for item in base.regime.probabilities
        ),
        AvailableEvidence(
            evidence_id="regime.trend_return",
            label="Regime trend return",
            value=format(features.trend_return, "f"),
        ),
        AvailableEvidence(
            evidence_id="regime.annualized_volatility",
            label="Regime annualized volatility",
            value=format(features.annualized_volatility, "f"),
        ),
        AvailableEvidence(
            evidence_id="regime.breadth",
            label="Market breadth",
            value=format(features.breadth, "f"),
        ),
        AvailableEvidence(
            evidence_id="regime.average_turnover",
            label="Average turnover",
            value=format(features.average_turnover, "f"),
        ),
        AvailableEvidence(
            evidence_id="regime.concentration",
            label="Market concentration",
            value=format(features.concentration, "f"),
        ),
        AvailableEvidence(
            evidence_id="regime.opening_return",
            label="Opening return",
            value=format(features.opening_return, "f"),
        ),
        AvailableEvidence(
            evidence_id="base.gross_exposure",
            label="Base target gross exposure",
            value=format(_gross(base.allocated.weights), "f"),
        ),
        AvailableEvidence(
            evidence_id="base.turnover",
            label="Base target turnover",
            value=format(base.allocated.turnover, "f"),
        ),
    )
    approved = tuple(
        strategy
        for strategy in StrategyKind
        if strategy in {proposal.strategy for proposal in base.strategy_proposals}
    )
    return AIRequestContext(
        base_decision_id=base.decision_id,
        snapshot_id=snapshot.snapshot_id,
        as_of=snapshot.as_of,
        decision_deadline=snapshot.execution_deadline,
        approved_strategies=approved,
        base_strategy_weights=allocations,
        base_target_weights=base.allocated.weights,
        base_cash_weight=base.allocated.cash_weight,
        base_turnover=base.allocated.turnover,
        max_strategy_weight_change=overlay_config.max_strategy_weight_change,
        max_overlay_turnover=overlay_config.max_overlay_turnover,
        max_target_gross_exposure=min(
            _gross(base.allocated.weights),
            overlay_config.allocation.max_gross_exposure,
        ),
        regime_label=base.regime.selected.value,
        regime_probabilities=tuple(
            (item.label.value, item.probability) for item in base.regime.probabilities
        ),
        evidence=evidence,
        source_payloads=snapshot.source_payloads,
    )


def build_ai_shadow_decision(
    base: DecisionRun,
    evaluation: AIOverlayEvaluation,
    *,
    prompt_version: str,
    overlay_version: str = AI_OVERLAY_VERSION,
) -> DecisionRun:
    configuration_id = canonical_sha256(
        {
            "base_configuration_id": base.configuration_id,
            "base_decision_id": base.decision_id,
            "ai_proposal_id": evaluation.proposal_id,
            "prompt_version": prompt_version,
            "overlay_version": overlay_version,
        }
    )
    pending = DecisionRun(
        decision_id="pending",
        snapshot_id=base.snapshot_id,
        configuration_id=configuration_id,
        pipeline_version=f"{base.pipeline_version}+ai-shadow-v1",
        features=base.features,
        regime=base.regime,
        strategy_proposals=base.strategy_proposals,
        proposal=base.proposal,
        allocated=evaluation.target,
        risk=evaluation.risk,
        execution=evaluation.execution,
        previous_allocation=base.previous_allocation,
        allocator_version=overlay_version,
    )
    decision_id = canonical_sha256(
        {
            "snapshot_id": pending.snapshot_id,
            "configuration_id": pending.configuration_id,
            "features": pending.features,
            "regime": pending.regime,
            "strategy_proposals": pending.strategy_proposals,
            "proposal": pending.proposal,
            "allocated": pending.allocated,
            "risk": pending.risk,
            "execution": pending.execution,
            "previous_allocation": pending.previous_allocation,
            "allocator_version": pending.allocator_version,
        }
    )
    return replace(pending, decision_id=decision_id)


def _normalized_allocations(draft: AIProposalDraft) -> tuple[StrategyAllocation, ...]:
    by_strategy = {
        StrategyKind(item.strategy): item.weight
        for item in draft.strategy_weights
        if item.weight > 0
    }
    return tuple(
        StrategyAllocation(strategy=strategy, weight=by_strategy[strategy])
        for strategy in StrategyKind
        if strategy in by_strategy
    )


def _target_turnover(
    base: TargetPortfolio,
    weights: tuple[TargetWeight, ...],
    cash_weight: Decimal,
) -> Decimal:
    before = {item.code: item.weight for item in base.weights}
    after = {item.code: item.weight for item in weights}
    absolute_change = sum(
        (
            abs(after.get(code, Decimal(0)) - before.get(code, Decimal(0)))
            for code in set(before) | set(after)
        ),
        Decimal(0),
    ) + abs(cash_weight - base.cash_weight)
    return (absolute_change / Decimal(2)).quantize(
        WEIGHT_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )


def _gross(weights: tuple[TargetWeight, ...]) -> Decimal:
    return sum((item.weight for item in weights), Decimal(0))


def _proposal_id(
    *,
    context: AIRequestContext,
    draft: AIProposalDraft | None,
    status: AIProposalStatus,
    errors: tuple[str, ...],
    version: str,
) -> str:
    return canonical_sha256(
        {
            "base_decision_id": context.base_decision_id,
            "snapshot_id": context.snapshot_id,
            "draft": draft,
            "status": status,
            "validation_errors": errors,
            "overlay_version": version,
        }
    )
