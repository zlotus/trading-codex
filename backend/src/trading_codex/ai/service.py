from __future__ import annotations

from typing import Protocol

from trading_codex.ai.contracts import AIClientResult, AIRequestContext
from trading_codex.ai.overlay import (
    AIShadowResult,
    BoundedAIOverlay,
    build_ai_context,
    build_ai_shadow_decision,
)
from trading_codex.domain.contracts import DecisionRun
from trading_codex.domain.models import DecisionSnapshot
from trading_codex.ledger.models import PortfolioTrack
from trading_codex.ledger.store import SQLiteLedger


class AIProposalClient(Protocol):
    async def generate(self, context: AIRequestContext) -> AIClientResult: ...


class AIShadowDecisionService:
    """Runs the bounded proposal path without exposing fills or mutable risk settings."""

    def __init__(
        self,
        ledger: SQLiteLedger,
        client: AIProposalClient,
        *,
        overlay: BoundedAIOverlay,
    ) -> None:
        self.ledger = ledger
        self.client = client
        self.overlay = overlay

    async def run(
        self,
        snapshot: DecisionSnapshot,
        base: DecisionRun,
    ) -> AIShadowResult:
        context = build_ai_context(
            snapshot,
            base,
            overlay_config=self.overlay.config,
        )
        client_result = await self.client.generate(context)
        evaluation = self.overlay.evaluate(snapshot, base, context, client_result)
        shadow = build_ai_shadow_decision(
            base,
            evaluation,
            prompt_version=client_result.prompt_version,
            overlay_version=self.overlay.version,
        )
        base_recorded_at = max(snapshot.as_of, client_result.requested_at)
        shadow_recorded_at = max(snapshot.as_of, client_result.completed_at)
        self.ledger.record_decision(
            snapshot,
            base,
            portfolio_track=PortfolioTrack.BASE,
            recorded_at=base_recorded_at,
        )
        self.ledger.record_decision(
            snapshot,
            shadow,
            portfolio_track=PortfolioTrack.AI_SHADOW,
            recorded_at=shadow_recorded_at,
        )
        self.ledger.record_ai_run(
            context=context,
            client=client_result,
            evaluation=evaluation,
            shadow_decision_id=shadow.decision_id,
            recorded_at=shadow_recorded_at,
        )
        return AIShadowResult(
            context=context,
            client=client_result,
            evaluation=evaluation,
            decision=shadow,
        )
