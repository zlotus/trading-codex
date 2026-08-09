from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import Any, Protocol
from uuid import uuid4

from trading_codex.ai.cache import CompletionCache
from trading_codex.ai.contracts import (
    AIClientConfig,
    AIClientResult,
    AICompletionOutcome,
    AIMessage,
    AIMessageRole,
    AIProposalDraft,
    AIRequestContext,
    CitedEvidence,
    DraftStrategyWeight,
    ProviderCompletion,
    StructuredCompletionRequest,
)
from trading_codex.domain.hashing import canonical_sha256


class LLMTransport(Protocol):
    async def complete(
        self,
        request: StructuredCompletionRequest,
        *,
        timeout_seconds: float,
    ) -> ProviderCompletion: ...


RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "decision_id",
        "snapshot_id",
        "generated_at",
        "valid_until",
        "summary",
        "rationale",
        "strategy_weights",
        "risk_scale",
        "evidence",
        "assistant_message",
    ],
    "properties": {
        "decision_id": {"type": "string"},
        "snapshot_id": {"type": "string"},
        "generated_at": {"type": "string", "format": "date-time"},
        "valid_until": {"type": "string", "format": "date-time"},
        "summary": {"type": "string", "maxLength": 800},
        "rationale": {"type": "string", "maxLength": 4000},
        "strategy_weights": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["strategy", "weight"],
                "properties": {
                    "strategy": {"type": "string"},
                    "weight": {"type": "string"},
                },
            },
        },
        "risk_scale": {"type": "string"},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["evidence_id", "claim"],
                "properties": {
                    "evidence_id": {"type": "string"},
                    "claim": {"type": "string"},
                },
            },
        },
        "assistant_message": {"type": "string", "maxLength": 4000},
    },
}


class ProviderNeutralLLMClient:
    def __init__(
        self,
        transport: LLMTransport,
        config: AIClientConfig,
        *,
        cache: CompletionCache | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport = transport
        self.config = config
        self.cache = cache
        self._now = now or (lambda: datetime.now(UTC))

    async def generate(self, context: AIRequestContext) -> AIClientResult:
        requested_at = _utc_now(self._now)
        context_payload = _request_payload(context, self.config)
        context_payload_json = json.dumps(
            context_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        messages = _messages(context_payload_json)
        request_payload = {
            "provider": self.config.provider,
            "model": self.config.model,
            "prompt_version": self.config.prompt_version,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in messages
            ],
            "response_schema": RESPONSE_SCHEMA,
            "budget": {
                "max_input_tokens": self.config.max_input_tokens,
                "max_output_tokens": self.config.max_output_tokens,
                "max_total_tokens": self.config.max_total_tokens,
                "timeout_seconds": format(self.config.timeout_seconds, ".9g"),
                "input_cost_per_million": format(
                    self.config.input_cost_per_million,
                    "f",
                ),
                "output_cost_per_million": format(
                    self.config.output_cost_per_million,
                    "f",
                ),
                "max_cost_usd": format(self.config.max_cost_usd, "f"),
            },
        }
        request_payload_json = json.dumps(
            request_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        cache_key = canonical_sha256(request_payload)
        request_id = canonical_sha256(
            {
                "cache_key": cache_key,
                "requested_at": requested_at,
                "attempt_id": uuid4().hex,
            }
        )
        estimated_input = ceil(len(request_payload_json.encode("utf-8")) / 4)
        if estimated_input > self.config.max_input_tokens:
            return self._failure(
                request_id=request_id,
                cache_key=cache_key,
                requested_at=requested_at,
                outcome=AICompletionOutcome.BUDGET_EXCEEDED,
                request_payload_json=request_payload_json,
                error=(
                    f"estimated input tokens {estimated_input} exceed "
                    f"budget {self.config.max_input_tokens}"
                ),
            )

        try:
            completion = self.cache.get(cache_key) if self.cache is not None else None
        except Exception as error:
            return self._failure(
                request_id=request_id,
                cache_key=cache_key,
                requested_at=requested_at,
                outcome=AICompletionOutcome.PROVIDER_ERROR,
                request_payload_json=request_payload_json,
                error=f"AI cache read failed: {_error_text(error)}",
            )
        cache_hit = completion is not None
        if completion is None:
            request = StructuredCompletionRequest(
                request_id=request_id,
                provider=self.config.provider,
                model=self.config.model,
                prompt_version=self.config.prompt_version,
                messages=messages,
                response_schema=RESPONSE_SCHEMA,
                max_output_tokens=self.config.max_output_tokens,
            )
            try:
                completion = await asyncio.wait_for(
                    self.transport.complete(
                        request,
                        timeout_seconds=self.config.timeout_seconds,
                    ),
                    timeout=self.config.timeout_seconds,
                )
            except TimeoutError:
                return self._failure(
                    request_id=request_id,
                    cache_key=cache_key,
                    requested_at=requested_at,
                    outcome=AICompletionOutcome.TIMEOUT,
                    request_payload_json=request_payload_json,
                    error=f"provider exceeded {self.config.timeout_seconds:g}s timeout",
                )
            except Exception as error:  # Provider adapters are outside the trust boundary.
                return self._failure(
                    request_id=request_id,
                    cache_key=cache_key,
                    requested_at=requested_at,
                    outcome=AICompletionOutcome.PROVIDER_ERROR,
                    request_payload_json=request_payload_json,
                    error=_error_text(error),
                )

        assert completion is not None
        if not isinstance(completion, ProviderCompletion):
            return self._failure(
                request_id=request_id,
                cache_key=cache_key,
                requested_at=requested_at,
                outcome=AICompletionOutcome.PROVIDER_ERROR,
                request_payload_json=request_payload_json,
                error="provider returned an invalid completion contract",
            )
        if completion.model != self.config.model:
            return self._failure(
                request_id=request_id,
                cache_key=cache_key,
                requested_at=requested_at,
                outcome=AICompletionOutcome.PROVIDER_ERROR,
                request_payload_json=request_payload_json,
                error="provider returned an unexpected model",
                completion=completion,
                cache_hit=cache_hit,
            )
        cost = _estimated_cost(completion, self.config)
        budget_error = _budget_error(completion, cost, self.config)
        if budget_error is not None:
            return self._failure(
                request_id=request_id,
                cache_key=cache_key,
                requested_at=requested_at,
                outcome=AICompletionOutcome.BUDGET_EXCEEDED,
                request_payload_json=request_payload_json,
                error=budget_error,
                completion=completion,
                cache_hit=cache_hit,
            )
        try:
            draft = _parse_draft(completion.content)
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            return self._failure(
                request_id=request_id,
                cache_key=cache_key,
                requested_at=requested_at,
                outcome=AICompletionOutcome.INVALID_OUTPUT,
                request_payload_json=request_payload_json,
                error=_error_text(error),
                completion=completion,
                cache_hit=cache_hit,
            )
        if not cache_hit and self.cache is not None:
            try:
                self.cache.put(cache_key, completion)
            except Exception as error:
                return self._failure(
                    request_id=request_id,
                    cache_key=cache_key,
                    requested_at=requested_at,
                    outcome=AICompletionOutcome.PROVIDER_ERROR,
                    request_payload_json=request_payload_json,
                    error=f"AI cache write failed: {_error_text(error)}",
                    completion=completion,
                )
        return AIClientResult(
            request_id=request_id,
            cache_key=cache_key,
            provider=self.config.provider,
            model=self.config.model,
            prompt_version=self.config.prompt_version,
            requested_at=requested_at,
            completed_at=_utc_now(self._now),
            outcome=AICompletionOutcome.SUCCEEDED,
            cache_hit=cache_hit,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            estimated_cost_usd=Decimal(0) if cache_hit else cost,
            request_payload_json=request_payload_json,
            response_payload_json=completion.content,
            provider_request_id=completion.provider_request_id,
            draft=draft,
            error=None,
        )

    def _failure(
        self,
        *,
        request_id: str,
        cache_key: str,
        requested_at: datetime,
        outcome: AICompletionOutcome,
        request_payload_json: str,
        error: str,
        completion: ProviderCompletion | None = None,
        cache_hit: bool = False,
    ) -> AIClientResult:
        return AIClientResult(
            request_id=request_id,
            cache_key=cache_key,
            provider=self.config.provider,
            model=self.config.model,
            prompt_version=self.config.prompt_version,
            requested_at=requested_at,
            completed_at=_utc_now(self._now),
            outcome=outcome,
            cache_hit=cache_hit,
            input_tokens=completion.input_tokens if completion is not None else 0,
            output_tokens=completion.output_tokens if completion is not None else 0,
            estimated_cost_usd=(
                Decimal(0)
                if completion is None or cache_hit
                else _estimated_cost(completion, self.config)
            ),
            request_payload_json=request_payload_json,
            response_payload_json=completion.content if completion is not None else None,
            provider_request_id=(
                completion.provider_request_id if completion is not None else None
            ),
            draft=None,
            error=error[:1000],
        )


def _messages(payload_json: str) -> tuple[AIMessage, ...]:
    return (
        AIMessage(
            role=AIMessageRole.SYSTEM,
            content=(
                "You are a bounded allocation reviewer. Return only JSON matching the "
                "provided schema. Use only approved strategies and cited evidence. You may "
                "reduce risk, but you must not propose orders, fills, risk-limit changes, or "
                "new strategies."
            ),
        ),
        AIMessage(role=AIMessageRole.USER, content=payload_json),
    )


def _request_payload(context: AIRequestContext, config: AIClientConfig) -> dict[str, object]:
    return {
        "contract": "bounded-ai-allocation-v1",
        "prompt_version": config.prompt_version,
        "decision_id": context.base_decision_id,
        "snapshot_id": context.snapshot_id,
        "as_of": context.as_of.isoformat(),
        "decision_deadline": context.decision_deadline.isoformat(),
        "approved_strategies": [item.value for item in context.approved_strategies],
        "base_strategy_weights": [
            {"strategy": item.strategy.value, "weight": format(item.weight, "f")}
            for item in context.base_strategy_weights
        ],
        "base_target_weights": [
            {"code": item.code, "weight": format(item.weight, "f"), "rank": item.rank}
            for item in context.base_target_weights
        ],
        "base_cash_weight": format(context.base_cash_weight, "f"),
        "base_turnover": format(context.base_turnover, "f"),
        "regime": {
            "selected": context.regime_label,
            "probabilities": [
                {"label": label, "probability": format(probability, "f")}
                for label, probability in context.regime_probabilities
            ],
        },
        "evidence": [
            {"evidence_id": item.evidence_id, "label": item.label, "value": item.value}
            for item in context.evidence
        ],
        "source_payloads": list(context.source_payloads),
        "limits": {
            "strategy_weights_sum": "1",
            "risk_scale_min": "0",
            "risk_scale_max": "1",
            "max_strategy_weight_change": format(
                context.max_strategy_weight_change,
                "f",
            ),
            "max_overlay_turnover": format(context.max_overlay_turnover, "f"),
            "max_target_gross_exposure": format(
                context.max_target_gross_exposure,
                "f",
            ),
            "max_output_tokens": config.max_output_tokens,
        },
    }


def _parse_draft(content: str) -> AIProposalDraft:
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("AI response must be a JSON object")
    expected = set(RESPONSE_SCHEMA["required"])
    if set(payload) != expected:
        raise ValueError("AI response fields do not match the structured contract")
    strategy_items = payload["strategy_weights"]
    evidence_items = payload["evidence"]
    if not isinstance(strategy_items, list) or not isinstance(evidence_items, list):
        raise TypeError("AI strategy weights and evidence must be arrays")
    strategies = tuple(
        DraftStrategyWeight(
            strategy=_strict_string(item, "strategy", keys={"strategy", "weight"}),
            weight=_strict_decimal(item, "weight", keys={"strategy", "weight"}),
        )
        for item in strategy_items
    )
    evidence = tuple(
        CitedEvidence(
            evidence_id=_strict_string(
                item,
                "evidence_id",
                keys={"evidence_id", "claim"},
            ),
            claim=_strict_string(item, "claim", keys={"evidence_id", "claim"}),
        )
        for item in evidence_items
    )
    return AIProposalDraft(
        decision_id=_strict_top_string(payload, "decision_id"),
        snapshot_id=_strict_top_string(payload, "snapshot_id"),
        generated_at=_parse_datetime(_strict_top_string(payload, "generated_at")),
        valid_until=_parse_datetime(_strict_top_string(payload, "valid_until")),
        summary=_strict_top_string(payload, "summary"),
        rationale=_strict_top_string(payload, "rationale"),
        strategy_weights=strategies,
        risk_scale=_top_decimal(payload, "risk_scale"),
        evidence=evidence,
        assistant_message=_strict_top_string(payload, "assistant_message"),
    )


def _strict_top_string(payload: dict[str, Any], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"AI response {field} must be a non-empty string")
    return value.strip()


def _top_decimal(payload: dict[str, Any], field: str) -> Decimal:
    value = payload[field]
    if not isinstance(value, str):
        raise TypeError(f"AI response {field} must be a decimal string")
    return Decimal(value)


def _strict_string(item: object, field: str, *, keys: set[str]) -> str:
    if not isinstance(item, dict) or set(item) != keys:
        raise ValueError("AI response array item fields are invalid")
    value = item[field]
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"AI response {field} must be a non-empty string")
    return value.strip()


def _strict_decimal(item: object, field: str, *, keys: set[str]) -> Decimal:
    value = _strict_string(item, field, keys=keys)
    return Decimal(value)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("AI timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def _budget_error(
    completion: ProviderCompletion,
    cost: Decimal,
    config: AIClientConfig,
) -> str | None:
    estimated_output = ceil(len(completion.content.encode("utf-8")) / 3)
    if estimated_output > config.max_output_tokens:
        return "locally estimated output token usage exceeds budget"
    if completion.input_tokens > config.max_input_tokens:
        return "provider input token usage exceeds budget"
    if completion.output_tokens > config.max_output_tokens:
        return "provider output token usage exceeds budget"
    if completion.input_tokens + completion.output_tokens > config.max_total_tokens:
        return "provider total token usage exceeds budget"
    if cost > config.max_cost_usd:
        return "provider estimated cost exceeds budget"
    return None


def _estimated_cost(completion: ProviderCompletion, config: AIClientConfig) -> Decimal:
    million = Decimal(1_000_000)
    return (
        Decimal(completion.input_tokens) * config.input_cost_per_million / million
        + Decimal(completion.output_tokens) * config.output_cost_per_million / million
    )


def _utc_now(now: Callable[[], datetime]) -> datetime:
    value = now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AI client clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _error_text(error: Exception) -> str:
    message = str(error).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__
