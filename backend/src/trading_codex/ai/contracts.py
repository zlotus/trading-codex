from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from trading_codex.domain.contracts import (
    ExecutionPlan,
    RiskDecision,
    StrategyAllocation,
    StrategyKind,
    TargetPortfolio,
    TargetWeight,
)
from trading_codex.domain.models import SnapshotValidationError


class AICompletionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROVIDER_ERROR = "provider_error"
    INVALID_OUTPUT = "invalid_output"


class AIProposalStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FALLBACK = "fallback"


class AIMessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class AIClientConfig:
    provider: str
    model: str
    prompt_version: str = "bounded-allocation-prompt-v1"
    max_input_tokens: int = 12_000
    max_output_tokens: int = 1_200
    max_total_tokens: int = 13_200
    timeout_seconds: float = 20.0
    input_cost_per_million: Decimal = Decimal(0)
    output_cost_per_million: Decimal = Decimal(0)
    max_cost_usd: Decimal = Decimal("0.50")

    def __post_init__(self) -> None:
        for name in ("provider", "model", "prompt_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"AI {name} is required")
        if self.max_input_tokens < 1 or self.max_output_tokens < 1:
            raise ValueError("AI token budgets must be positive")
        if self.max_total_tokens < self.max_input_tokens:
            raise ValueError("AI total token budget cannot be smaller than input budget")
        if self.timeout_seconds <= 0:
            raise ValueError("AI timeout must be positive")
        if self.input_cost_per_million < 0 or self.output_cost_per_million < 0:
            raise ValueError("AI token prices must be non-negative")
        if self.max_cost_usd < 0:
            raise ValueError("AI cost budget must be non-negative")


@dataclass(frozen=True)
class AIMessage:
    role: AIMessageRole
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, AIMessageRole) or not self.content:
            raise ValueError("AI message role and content are required")


@dataclass(frozen=True)
class StructuredCompletionRequest:
    request_id: str
    provider: str
    model: str
    prompt_version: str
    messages: tuple[AIMessage, ...]
    response_schema: dict[str, object]
    max_output_tokens: int


@dataclass(frozen=True)
class ProviderCompletion:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.content, str)
            or not self.content.strip()
            or not isinstance(self.model, str)
            or not self.model.strip()
        ):
            raise ValueError("provider completion content and model are required")
        if type(self.input_tokens) is not int or type(self.output_tokens) is not int:
            raise ValueError("provider token usage must use integers")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("provider token usage must be non-negative")
        if self.provider_request_id is not None and (
            not isinstance(self.provider_request_id, str)
            or not self.provider_request_id.strip()
        ):
            raise ValueError("provider request id must be a non-empty string")


@dataclass(frozen=True)
class AvailableEvidence:
    evidence_id: str
    label: str
    value: str

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.label or not self.value:
            raise SnapshotValidationError("AI evidence id, label, and value are required")
        if len(self.evidence_id) > 200 or len(self.label) > 200 or len(self.value) > 2_000:
            raise SnapshotValidationError("AI available evidence exceeds its size limit")


@dataclass(frozen=True)
class CitedEvidence:
    evidence_id: str
    claim: str

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.claim:
            raise SnapshotValidationError("AI evidence citation and claim are required")
        if len(self.evidence_id) > 200 or len(self.claim) > 1_000:
            raise SnapshotValidationError("AI evidence citation exceeds its size limit")


@dataclass(frozen=True)
class DraftStrategyWeight:
    strategy: str
    weight: Decimal

    def __post_init__(self) -> None:
        if not self.strategy:
            raise SnapshotValidationError("AI strategy name is required")
        if not self.weight.is_finite() or not Decimal(0) <= self.weight <= Decimal(1):
            raise SnapshotValidationError("AI strategy weight must be in [0, 1]")


@dataclass(frozen=True)
class AIRequestContext:
    base_decision_id: str
    snapshot_id: str
    as_of: datetime
    decision_deadline: datetime
    approved_strategies: tuple[StrategyKind, ...]
    base_strategy_weights: tuple[StrategyAllocation, ...]
    base_target_weights: tuple[TargetWeight, ...]
    base_cash_weight: Decimal
    base_turnover: Decimal
    max_strategy_weight_change: Decimal
    max_overlay_turnover: Decimal
    max_target_gross_exposure: Decimal
    regime_label: str
    regime_probabilities: tuple[tuple[str, Decimal], ...]
    evidence: tuple[AvailableEvidence, ...]
    source_payloads: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _aware_utc(self.as_of, field="AI context as_of"))
        object.__setattr__(
            self,
            "decision_deadline",
            _aware_utc(self.decision_deadline, field="AI context decision_deadline"),
        )
        if self.decision_deadline <= self.as_of:
            raise SnapshotValidationError("AI decision deadline must follow as_of")
        if not self.base_decision_id or not self.snapshot_id:
            raise SnapshotValidationError("AI context decision and snapshot ids are required")
        if not self.approved_strategies or len(self.approved_strategies) != len(
            set(self.approved_strategies)
        ):
            raise SnapshotValidationError("AI approved strategies must be non-empty and unique")
        strategy_names = tuple(item.strategy for item in self.base_strategy_weights)
        if len(strategy_names) != len(set(strategy_names)):
            raise SnapshotValidationError("AI base strategy weights must be unique")
        if any(strategy not in self.approved_strategies for strategy in strategy_names):
            raise SnapshotValidationError("AI base allocation uses an unapproved strategy")
        if sum((item.weight for item in self.base_strategy_weights), Decimal(0)) != Decimal(1):
            raise SnapshotValidationError("AI base strategy weights must sum to one")
        codes = tuple(item.code for item in self.base_target_weights)
        if codes != tuple(sorted(set(codes))):
            raise SnapshotValidationError("AI base target weights must be sorted and unique")
        if any(item.weight <= 0 for item in self.base_target_weights):
            raise SnapshotValidationError("AI base target weights must be positive")
        base_gross = sum(
            (item.weight for item in self.base_target_weights),
            Decimal(0),
        )
        if (
            base_gross > Decimal(1)
            or self.base_cash_weight != Decimal(1) - base_gross
        ):
            raise SnapshotValidationError("AI base target exposure is inconsistent")
        if not Decimal(0) <= self.base_turnover <= Decimal(1):
            raise SnapshotValidationError("AI base turnover must be in [0, 1]")
        for name in (
            "max_strategy_weight_change",
            "max_overlay_turnover",
            "max_target_gross_exposure",
        ):
            value = getattr(self, name)
            if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
                raise SnapshotValidationError(f"AI context {name} must be in [0, 1]")
        if self.max_target_gross_exposure > base_gross:
            raise SnapshotValidationError("AI gross limit cannot exceed base exposure")
        regime_labels = tuple(label for label, _ in self.regime_probabilities)
        if (
            not regime_labels
            or len(regime_labels) != len(set(regime_labels))
            or self.regime_label not in regime_labels
        ):
            raise SnapshotValidationError("AI regime probabilities are incomplete")
        if any(
            not probability.is_finite()
            or not Decimal(0) <= probability <= Decimal(1)
            for _, probability in self.regime_probabilities
        ) or sum(
            (probability for _, probability in self.regime_probabilities),
            Decimal(0),
        ) != Decimal(1):
            raise SnapshotValidationError("AI regime probabilities must sum to one")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if not evidence_ids or len(evidence_ids) != len(set(evidence_ids)):
            raise SnapshotValidationError("AI evidence ids must be non-empty and unique")


@dataclass(frozen=True)
class AIProposalDraft:
    decision_id: str
    snapshot_id: str
    generated_at: datetime
    valid_until: datetime
    summary: str
    rationale: str
    strategy_weights: tuple[DraftStrategyWeight, ...]
    risk_scale: Decimal
    evidence: tuple[CitedEvidence, ...]
    assistant_message: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _aware_utc(self.generated_at, field="AI proposal generated_at"),
        )
        object.__setattr__(
            self,
            "valid_until",
            _aware_utc(self.valid_until, field="AI proposal valid_until"),
        )
        if not self.decision_id or not self.snapshot_id:
            raise SnapshotValidationError("AI proposal decision and snapshot ids are required")
        for name, maximum in (
            ("summary", 800),
            ("rationale", 4_000),
            ("assistant_message", 4_000),
        ):
            value = getattr(self, name)
            if not value or len(value) > maximum:
                raise SnapshotValidationError(
                    f"AI proposal {name} must contain 1 to {maximum} characters"
                )
        names = tuple(item.strategy for item in self.strategy_weights)
        if not names or len(names) > 4 or len(names) != len(set(names)):
            raise SnapshotValidationError("AI proposal strategies must be non-empty and unique")
        if not self.risk_scale.is_finite() or not Decimal(0) <= self.risk_scale <= Decimal(1):
            raise SnapshotValidationError("AI risk scale must be in [0, 1]")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if (
            not evidence_ids
            or len(evidence_ids) > 12
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            raise SnapshotValidationError("AI proposal evidence must be non-empty and unique")


@dataclass(frozen=True)
class AIClientResult:
    request_id: str
    cache_key: str
    provider: str
    model: str
    prompt_version: str
    requested_at: datetime
    completed_at: datetime
    outcome: AICompletionOutcome
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
    request_payload_json: str
    response_payload_json: str | None
    provider_request_id: str | None
    draft: AIProposalDraft | None
    error: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requested_at",
            _aware_utc(self.requested_at, field="AI request requested_at"),
        )
        object.__setattr__(
            self,
            "completed_at",
            _aware_utc(self.completed_at, field="AI request completed_at"),
        )
        if self.completed_at < self.requested_at:
            raise SnapshotValidationError("AI completion cannot precede its request")
        if self.input_tokens < 0 or self.output_tokens < 0 or self.estimated_cost_usd < 0:
            raise SnapshotValidationError("AI usage and cost must be non-negative")
        if self.outcome is AICompletionOutcome.SUCCEEDED:
            if self.draft is None or self.error is not None:
                raise SnapshotValidationError("successful AI completion must contain a draft")
        elif self.draft is not None or not self.error:
            raise SnapshotValidationError("failed AI completion must contain only an error")


@dataclass(frozen=True)
class AIOverlayEvaluation:
    proposal_id: str
    status: AIProposalStatus
    summary: str
    rationale: str
    strategy_weights: tuple[StrategyAllocation, ...]
    risk_scale: Decimal
    evidence: tuple[CitedEvidence, ...]
    validation_errors: tuple[str, ...]
    assistant_message: str
    target: TargetPortfolio
    risk: RiskDecision
    execution: ExecutionPlan

    def __post_init__(self) -> None:
        if not self.proposal_id or not self.summary or not self.rationale:
            raise SnapshotValidationError("AI overlay identity and explanation are required")
        if not self.assistant_message:
            raise SnapshotValidationError("AI overlay assistant message is required")
        if not self.risk_scale.is_finite() or not Decimal(0) <= self.risk_scale <= Decimal(1):
            raise SnapshotValidationError("AI overlay risk scale must be in [0, 1]")
        if sum((item.weight for item in self.strategy_weights), Decimal(0)) != Decimal(1):
            raise SnapshotValidationError("AI overlay strategy weights must sum to one")
        if self.risk.requested != self.target:
            raise SnapshotValidationError("AI overlay risk decision must reference its target")
        if self.risk.snapshot_id != self.target.snapshot_id:
            raise SnapshotValidationError("AI overlay risk decision uses a different snapshot")
        if self.execution.snapshot_id != self.target.snapshot_id:
            raise SnapshotValidationError("AI overlay execution uses a different snapshot")


@dataclass(frozen=True)
class AIMessageView:
    message_id: str
    role: AIMessageRole
    content: str
    created_at: datetime


@dataclass(frozen=True)
class AIRunView:
    run_id: str
    request_id: str
    proposal_id: str
    base_decision_id: str
    shadow_decision_id: str
    snapshot_id: str
    status: AIProposalStatus
    outcome: AICompletionOutcome
    provider: str
    model: str
    prompt_version: str
    requested_at: datetime
    completed_at: datetime
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
    summary: str
    rationale: str
    strategy_weights: tuple[StrategyAllocation, ...]
    risk_scale: Decimal
    evidence: tuple[CitedEvidence, ...]
    validation_errors: tuple[str, ...]
    base_target_weights: tuple[TargetWeight, ...]
    shadow_target_weights: tuple[TargetWeight, ...]
    messages: tuple[AIMessageView, ...]


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SnapshotValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
