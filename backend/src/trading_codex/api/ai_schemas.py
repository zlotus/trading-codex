from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trading_codex.ai.contracts import (
    AICompletionOutcome,
    AIMessageRole,
    AIProposalStatus,
)
from trading_codex.domain.contracts import StrategyKind


class AIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AIStrategyWeightResponse(AIModel):
    strategy: StrategyKind
    weight: Decimal


class AITargetWeightResponse(AIModel):
    code: str
    weight: Decimal
    rank: int


class AIEvidenceResponse(AIModel):
    evidence_id: str
    claim: str


class AIMessageResponse(AIModel):
    message_id: str
    role: AIMessageRole
    content: str
    created_at: datetime


class AIRunResponse(AIModel):
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
    strategy_weights: list[AIStrategyWeightResponse]
    risk_scale: Decimal
    evidence: list[AIEvidenceResponse]
    validation_errors: list[str]
    base_target_weights: list[AITargetWeightResponse]
    shadow_target_weights: list[AITargetWeightResponse]
    messages: list[AIMessageResponse]


class AIWorkspaceResponse(AIModel):
    core_state: Literal["ready"] = "ready"
    provider_configured: bool
    latest: AIRunResponse | None
