import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from trading_codex.ai.cache import FileCompletionCache, MemoryCompletionCache
from trading_codex.ai.client import ProviderNeutralLLMClient
from trading_codex.ai.contracts import (
    AIClientConfig,
    AICompletionOutcome,
    AIProposalStatus,
    AIRequestContext,
    ProviderCompletion,
    StructuredCompletionRequest,
)
from trading_codex.ai.overlay import BoundedAIOverlay, build_ai_context
from trading_codex.ai.research import (
    IsolatedResearchDataset,
    OfflineResearchRunner,
    ResearchIsolationError,
    ResearchPartition,
    ResearchSplit,
    directory_sha256,
)
from trading_codex.ai.service import AIShadowDecisionService
from trading_codex.api.dependencies import get_ledger
from trading_codex.domain.contracts import DecisionRun
from trading_codex.domain.hashing import canonical_sha256
from trading_codex.domain.models import (
    DailyBar,
    DecisionPoint,
    DecisionSnapshot,
    InstrumentRule,
    OpeningBar,
    RiskValidationError,
    SnapshotValidationError,
)
from trading_codex.domain.pipeline import DecisionPipeline, DecisionPipelineConfig
from trading_codex.ledger.models import LedgerInvariantError, PortfolioTrack
from trading_codex.ledger.store import SQLiteLedger
from trading_codex.main import app


def _snapshot() -> DecisionSnapshot:
    start = date(2024, 1, 1)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(10))
    bars = []
    for code_index, code in enumerate(codes):
        previous_execution = None
        for index in range(26):
            day = start + timedelta(days=index)
            signal = Decimal(10 + code_index) * (
                Decimal(1) + Decimal("0.004") * Decimal(index)
            )
            execution = signal + Decimal(5)
            bars.append(
                DailyBar(
                    code=code,
                    trade_date=day,
                    signal_close=signal,
                    execution_close=execution,
                    previous_close=previous_execution or execution,
                    volume=100_000,
                    trade_status=True,
                    is_st=False,
                    available_at=datetime(day.year, day.month, day.day, 7, tzinfo=UTC),
                    amount=Decimal(1_000_000 + code_index * 10_000),
                    turnover=Decimal("0.02") + Decimal(code_index) / Decimal(10_000),
                )
            )
            previous_execution = execution
    history_end = start + timedelta(days=25)
    decision_date = history_end + timedelta(days=1)
    as_of = datetime(2024, 1, 27, 1, 35, tzinfo=UTC)
    previous_by_code = {
        bar.code: bar.execution_close for bar in bars if bar.trade_date == history_end
    }
    return DecisionSnapshot(
        as_of=as_of,
        decision_date=decision_date,
        execution_deadline=as_of + timedelta(minutes=30),
        cash=Decimal("1000000"),
        candidate_codes=codes,
        bars=tuple(sorted(bars, key=lambda bar: (bar.code, bar.trade_date))),
        positions=(),
        rules=tuple(
            InstrumentRule(code=code, lot_size=100, price_limit_ratio=Decimal("0.10"))
            for code in codes
        ),
        source_payloads=(canonical_sha256({"fixture": "ai-shadow"}),),
        decision_point=DecisionPoint.OPENING_0935,
        regime_codes=codes,
        opening_bars=tuple(
            OpeningBar(
                code=code,
                timestamp=as_of,
                open_price=Decimal(10 + code_index),
                close_price=Decimal(10 + code_index) * Decimal("1.005"),
                previous_close=previous_by_code[code],
                volume=10_000,
                amount=Decimal(100_000 + code_index * 1_000),
                trade_status=True,
                is_st=False,
                available_at=as_of,
            )
            for code_index, code in enumerate(codes)
        ),
    )


def _completion_payload(
    decision_id: str,
    snapshot_id: str,
    *,
    generated_at: datetime,
    valid_until: datetime,
    strategy_weights: list[dict[str, str]] | None = None,
    evidence_id: str = "regime.selected",
) -> str:
    return json.dumps(
        {
            "decision_id": decision_id,
            "snapshot_id": snapshot_id,
            "generated_at": generated_at.isoformat(),
            "valid_until": valid_until.isoformat(),
            "summary": "开盘状态偏强，保留主策略并小幅提高现金。",
            "rationale": "风险状态证据支持维持动量，但用现金降低组合暴露。",
            "strategy_weights": strategy_weights
            or [
                {"strategy": "momentum", "weight": "0.90"},
                {"strategy": "cash", "weight": "0.10"},
            ],
            "risk_scale": "1",
            "evidence": [
                {"evidence_id": evidence_id, "claim": "当前状态为 risk_on。"}
            ],
            "assistant_message": "建议在影子组合中保留动量并配置 10% 现金。",
        },
        ensure_ascii=False,
    )


class FixtureTransport:
    def __init__(self, content: str, *, delay: float = 0) -> None:
        self.content = content
        self.delay = delay
        self.calls = 0
        self.requests: list[StructuredCompletionRequest] = []
        self.timeouts: list[float] = []

    async def complete(
        self,
        request: StructuredCompletionRequest,
        *,
        timeout_seconds: float,
    ) -> ProviderCompletion:
        self.calls += 1
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)
        if self.delay:
            await asyncio.sleep(self.delay)
        return ProviderCompletion(
            content=self.content,
            model=request.model,
            input_tokens=800,
            output_tokens=180,
            provider_request_id="fixture-request",
        )


def _client(
    transport: FixtureTransport,
    now: datetime,
    *,
    cache: MemoryCompletionCache | None = None,
    timeout_seconds: float = 1,
    max_input_tokens: int = 12_000,
) -> ProviderNeutralLLMClient:
    return ProviderNeutralLLMClient(
        transport,
        AIClientConfig(
            provider="fixture",
            model="fixture-model",
            timeout_seconds=timeout_seconds,
            max_input_tokens=max_input_tokens,
            max_output_tokens=1_200,
            max_total_tokens=max(max_input_tokens + 1_200, 1_201),
            input_cost_per_million=Decimal("1"),
            output_cost_per_million=Decimal("2"),
        ),
        cache=cache,
        now=lambda: now,
    )


def _pipeline_context(
    snapshot: DecisionSnapshot,
) -> tuple[DecisionPipeline, DecisionRun, BoundedAIOverlay, AIRequestContext]:
    pipeline = DecisionPipeline()
    base = pipeline.run(snapshot)
    overlay = BoundedAIOverlay.from_pipeline(pipeline)
    context = build_ai_context(
        snapshot,
        base,
        overlay_config=overlay.config,
    )
    return pipeline, base, overlay, context


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_provider_neutral_client_enforces_schema_budget_and_cache() -> None:
    snapshot = _snapshot()
    _, base, _, context = _pipeline_context(snapshot)
    completed_at = snapshot.as_of + timedelta(minutes=1)
    transport = FixtureTransport(
        _completion_payload(
            base.decision_id,
            snapshot.snapshot_id,
            generated_at=completed_at,
            valid_until=snapshot.execution_deadline,
        )
    )
    cache = MemoryCompletionCache()
    client = _client(transport, completed_at, cache=cache)

    first = await client.generate(context)
    second = await client.generate(context)
    changed_budget = await _client(
        transport,
        completed_at,
        cache=cache,
        max_input_tokens=13_000,
    ).generate(context)

    assert first.outcome is AICompletionOutcome.SUCCEEDED
    assert first.cache_hit is False
    assert first.estimated_cost_usd == Decimal("0.00116")
    assert second.outcome is AICompletionOutcome.SUCCEEDED
    assert second.cache_hit is True
    assert second.estimated_cost_usd == 0
    assert changed_budget.cache_key != first.cache_key
    assert changed_budget.cache_hit is False
    assert transport.calls == 2
    assert transport.timeouts == [1, 1]
    assert transport.requests[0].prompt_version == "bounded-allocation-prompt-v1"
    assert transport.requests[0].response_schema["additionalProperties"] is False


@pytest.mark.anyio
async def test_client_timeout_and_preflight_budget_fail_closed() -> None:
    snapshot = _snapshot()
    _, base, _, context = _pipeline_context(snapshot)
    now = snapshot.as_of + timedelta(minutes=1)
    content = _completion_payload(
        base.decision_id,
        snapshot.snapshot_id,
        generated_at=now,
        valid_until=snapshot.execution_deadline,
    )
    slow_transport = FixtureTransport(content, delay=0.05)
    timeout = await _client(
        slow_transport,
        now,
        timeout_seconds=0.001,
    ).generate(context)
    assert timeout.outcome is AICompletionOutcome.TIMEOUT
    assert timeout.draft is None

    untouched_transport = FixtureTransport(content)
    over_budget = await _client(
        untouched_transport,
        now,
        max_input_tokens=1,
    ).generate(context)
    assert over_budget.outcome is AICompletionOutcome.BUDGET_EXCEEDED
    assert untouched_transport.calls == 0

    class InvalidTransport:
        async def complete(self, request, *, timeout_seconds):
            return {"content": "{}"}

    invalid_contract = await _client(InvalidTransport(), now).generate(context)
    assert invalid_contract.outcome is AICompletionOutcome.PROVIDER_ERROR
    assert invalid_contract.error == "provider returned an invalid completion contract"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("strategy_weights", "evidence_id", "expected"),
    [
        (
            [{"strategy": "unapproved", "weight": "1"}],
            "regime.selected",
            "unknown strategy",
        ),
        (
            [
                {"strategy": "momentum", "weight": "0.70"},
                {"strategy": "cash", "weight": "0.30"},
            ],
            "regime.selected",
            "weight change exceeds",
        ),
        (
            [
                {"strategy": "momentum", "weight": "0.90"},
                {"strategy": "cash", "weight": "0.10"},
            ],
            "external.news",
            "unknown evidence",
        ),
    ],
)
async def test_overlay_rejects_unknown_or_out_of_bounds_proposals(
    strategy_weights: list[dict[str, str]],
    evidence_id: str,
    expected: str,
) -> None:
    snapshot = _snapshot()
    _, base, overlay, context = _pipeline_context(snapshot)
    now = snapshot.as_of + timedelta(minutes=1)
    transport = FixtureTransport(
        _completion_payload(
            base.decision_id,
            snapshot.snapshot_id,
            generated_at=now,
            valid_until=snapshot.execution_deadline,
            strategy_weights=strategy_weights,
            evidence_id=evidence_id,
        )
    )
    result = await _client(transport, now).generate(context)

    evaluation = overlay.evaluate(snapshot, base, context, result)

    assert evaluation.status is AIProposalStatus.REJECTED
    assert evaluation.target.weights == base.allocated.weights
    assert evaluation.execution == base.execution
    assert any(expected in error for error in evaluation.validation_errors)


@pytest.mark.anyio
async def test_overlay_rejects_late_proposal_and_timeout_falls_back() -> None:
    snapshot = _snapshot()
    _, base, overlay, context = _pipeline_context(snapshot)
    late = snapshot.execution_deadline
    late_client = _client(
        FixtureTransport(
            _completion_payload(
                base.decision_id,
                snapshot.snapshot_id,
                generated_at=snapshot.as_of + timedelta(minutes=1),
                valid_until=snapshot.execution_deadline,
            )
        ),
        late,
    )
    late_result = await late_client.generate(context)
    late_evaluation = overlay.evaluate(
        snapshot,
        base,
        context,
        late_result,
    )
    assert late_evaluation.status is AIProposalStatus.REJECTED
    assert any("deadline" in error for error in late_evaluation.validation_errors)

    timeout_result = await _client(
        FixtureTransport("{}", delay=0.05),
        snapshot.as_of + timedelta(minutes=1),
        timeout_seconds=0.001,
    ).generate(context)
    fallback = overlay.evaluate(snapshot, base, context, timeout_result)
    assert fallback.status is AIProposalStatus.FALLBACK
    assert fallback.target.weights == base.allocated.weights
    assert fallback.risk.requested == fallback.target


@pytest.mark.anyio
async def test_overlay_falls_back_when_deterministic_execution_rejects_target() -> None:
    snapshot = _snapshot()
    _, base, overlay, context = _pipeline_context(snapshot)
    now = snapshot.as_of + timedelta(minutes=1)
    result = await _client(
        FixtureTransport(
            _completion_payload(
                base.decision_id,
                snapshot.snapshot_id,
                generated_at=now,
                valid_until=snapshot.execution_deadline,
            )
        ),
        now,
    ).generate(context)

    class RejectingPlanner:
        def plan(self, snapshot, risk):
            raise RiskValidationError("fixture execution rejection")

    overlay.execution_planner = RejectingPlanner()
    evaluation = overlay.evaluate(snapshot, base, context, result)

    assert evaluation.status is AIProposalStatus.REJECTED
    assert evaluation.target.weights == base.allocated.weights
    assert evaluation.execution == base.execution
    assert evaluation.validation_errors == (
        "deterministic risk/execution rejected AI target: fixture execution rejection",
    )


@pytest.mark.anyio
async def test_overlay_requires_the_exact_base_pipeline_configuration() -> None:
    snapshot = _snapshot()
    _, base, overlay, context = _pipeline_context(snapshot)
    now = snapshot.as_of + timedelta(minutes=1)
    result = await _client(
        FixtureTransport(
            _completion_payload(
                base.decision_id,
                snapshot.snapshot_id,
                generated_at=now,
                valid_until=snapshot.execution_deadline,
            )
        ),
        now,
    ).generate(context)
    mismatched = BoundedAIOverlay.from_pipeline(
        DecisionPipeline(DecisionPipelineConfig(version="mismatched-pipeline-v1"))
    )

    with pytest.raises(SnapshotValidationError, match="configuration differs"):
        mismatched.evaluate(snapshot, base, context, result)
    with pytest.raises(SnapshotValidationError, match="limits are inconsistent"):
        overlay.evaluate(
            snapshot,
            base,
            replace(context, max_overlay_turnover=Decimal("0.09")),
            result,
        )


@pytest.mark.anyio
async def test_ai_shadow_service_records_separate_append_only_audit(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    _, base, overlay, _ = _pipeline_context(snapshot)
    now = snapshot.as_of + timedelta(minutes=1)
    transport = FixtureTransport(
        _completion_payload(
            base.decision_id,
            snapshot.snapshot_id,
            generated_at=now,
            valid_until=snapshot.execution_deadline,
        )
    )
    ledger = SQLiteLedger(tmp_path / "ai-ledger.db")
    service = AIShadowDecisionService(
        ledger,
        _client(transport, now),
        overlay=overlay,
    )

    result = await service.run(snapshot, base)
    latest = ledger.latest_ai_run(as_of=now)

    assert result.evaluation.status is AIProposalStatus.ACCEPTED
    assert result.evaluation.target.weights != base.allocated.weights
    assert latest is not None
    assert latest.base_decision_id == base.decision_id
    assert latest.shadow_decision_id == result.decision.decision_id
    assert latest.base_target_weights == base.allocated.weights
    assert latest.shadow_target_weights == result.evaluation.target.weights
    assert latest.messages[0].content == result.evaluation.assistant_message
    repeated = await service.run(snapshot, base)
    repeated_latest = ledger.latest_ai_run(as_of=now)
    assert repeated.client.request_id != result.client.request_id
    assert repeated_latest is not None
    assert repeated_latest.run_id != latest.run_id
    assert len(repeated_latest.messages) == 1
    assert repeated_latest.messages[0].content == repeated.evaluation.assistant_message
    forged_weights = (
        replace(
            result.context.base_target_weights[0],
            weight=result.context.base_target_weights[0].weight - Decimal("0.01"),
        ),
        *result.context.base_target_weights[1:],
    )
    forged_context = replace(
        result.context,
        base_target_weights=forged_weights,
        base_cash_weight=result.context.base_cash_weight + Decimal("0.01"),
        max_target_gross_exposure=(
            result.context.max_target_gross_exposure - Decimal("0.01")
        ),
    )
    with pytest.raises(LedgerInvariantError, match="disagrees with base"):
        ledger.record_ai_run(
            context=forged_context,
            client=result.client,
            evaluation=result.evaluation,
            shadow_decision_id=result.decision.decision_id,
            recorded_at=now,
        )
    assert ledger.latest_allocation_state(
        before=now + timedelta(seconds=1),
        portfolio_track=PortfolioTrack.BASE,
    ) is not None
    assert ledger.latest_allocation_state(
        before=now + timedelta(seconds=1),
        portfolio_track=PortfolioTrack.AI_SHADOW,
    ) is not None

    with sqlite3.connect(tmp_path / "ai-ledger.db") as connection:
        tracks = connection.execute(
            "SELECT portfolio_track, COUNT(*) FROM decision_runs GROUP BY portfolio_track"
        ).fetchall()
        assert dict(tracks) == {"ai_shadow": 1, "base": 1}
        assert connection.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM ai_messages").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(DISTINCT run_id) FROM ai_messages"
            ).fetchone()[0]
            == 2
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE ai_runs SET status = 'fallback'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM ai_messages")


@pytest.mark.anyio
async def test_ai_shadow_service_audits_timeout_as_unchanged_fallback(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    _, base, overlay, _ = _pipeline_context(snapshot)
    now = snapshot.as_of + timedelta(minutes=1)
    ledger = SQLiteLedger(tmp_path / "fallback-ledger.db")
    service = AIShadowDecisionService(
        ledger,
        _client(
            FixtureTransport("{}", delay=0.05),
            now,
            timeout_seconds=0.001,
        ),
        overlay=overlay,
    )

    result = await service.run(snapshot, base)
    latest = ledger.latest_ai_run(as_of=now)

    assert result.evaluation.status is AIProposalStatus.FALLBACK
    assert result.evaluation.target.weights == base.allocated.weights
    assert latest is not None
    assert latest.status is AIProposalStatus.FALLBACK
    assert latest.outcome is AICompletionOutcome.TIMEOUT
    assert latest.base_target_weights == latest.shadow_target_weights
    assert latest.validation_errors[0].startswith("timeout:")


@pytest.mark.anyio
async def test_ai_workspace_api_is_read_only_and_returns_latest_run(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    _, base, overlay, _ = _pipeline_context(snapshot)
    now = snapshot.as_of + timedelta(minutes=1)
    ledger = SQLiteLedger(tmp_path / "api-ai-ledger.db")
    service = AIShadowDecisionService(
        ledger,
        _client(
            FixtureTransport(
                _completion_payload(
                    base.decision_id,
                    snapshot.snapshot_id,
                    generated_at=now,
                    valid_until=snapshot.execution_deadline,
                )
            ),
            now,
        ),
        overlay=overlay,
    )
    await service.run(snapshot, base)

    async def override_ledger() -> SQLiteLedger:
        return ledger

    app.dependency_overrides[get_ledger] = override_ledger
    transport = httpx2.ASGITransport(app=app)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/ai/workspace")
            invalid_as_of = await client.get(
                "/api/v1/ai/workspace",
                params={"as_of": "2026-08-09T02:00:00"},
            )
            openapi = (await client.get("/openapi.json")).json()
    finally:
        app.dependency_overrides.pop(get_ledger, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["core_state"] == "ready"
    assert payload["latest"]["status"] == "accepted"
    assert payload["latest"]["strategy_weights"] == [
        {"strategy": "momentum", "weight": "0.90"},
        {"strategy": "cash", "weight": "0.10"},
    ]
    assert invalid_as_of.status_code == 422
    assert invalid_as_of.json()["detail"] == "as_of must be timezone-aware"
    assert set(openapi["paths"]["/api/v1/ai/workspace"]) == {"get"}


def test_file_completion_cache_is_immutable(tmp_path: Path) -> None:
    cache = FileCompletionCache(tmp_path / "cache")
    key = "a" * 64
    completion = ProviderCompletion(
        content="{}",
        model="fixture-model",
        input_tokens=1,
        output_tokens=1,
    )
    cache.put(key, completion)
    cache.put(key, completion)
    assert cache.get(key) == completion
    with pytest.raises(ValueError, match="different content"):
        cache.put(key, replace(completion, content='{"changed":true}'))
    cache_path = tmp_path / "cache" / key[:2] / f"{key}.json"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["completion"]["input_tokens"] = "1"
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        cache.get(key)


def test_offline_research_hides_test_until_candidate_is_frozen(tmp_path: Path) -> None:
    roots = {}
    for split, content in (
        (ResearchSplit.TRAIN, "train"),
        (ResearchSplit.VALIDATION, "validation"),
        (ResearchSplit.TEST, "test"),
    ):
        root = tmp_path / split.value
        root.mkdir()
        (root / "data.json").write_text(content, encoding="utf-8")
        roots[split] = root
    dataset = IsolatedResearchDataset(
        train=ResearchPartition(
            split=ResearchSplit.TRAIN,
            root=roots[ResearchSplit.TRAIN],
            start_date=date(2020, 1, 1),
            end_date=date(2021, 12, 31),
            content_sha256=directory_sha256(roots[ResearchSplit.TRAIN]),
        ),
        validation=ResearchPartition(
            split=ResearchSplit.VALIDATION,
            root=roots[ResearchSplit.VALIDATION],
            start_date=date(2022, 1, 1),
            end_date=date(2022, 12, 31),
            content_sha256=directory_sha256(roots[ResearchSplit.VALIDATION]),
        ),
        test=ResearchPartition(
            split=ResearchSplit.TEST,
            root=roots[ResearchSplit.TEST],
            start_date=date(2023, 1, 1),
            end_date=date(2023, 12, 31),
            content_sha256=directory_sha256(roots[ResearchSplit.TEST]),
        ),
    )
    seen = {}

    class Adapter:
        def develop(self, development):
            assert not hasattr(development, "test")
            seen["development"] = (development.train.root, development.validation.root)
            return {"parameter": "frozen-v1"}

        def evaluate(self, candidate, sealed_test):
            seen["candidate"] = candidate.candidate_sha256
            seen["test"] = sealed_test.test.root
            return {"score": "0.12"}

    runner = OfflineResearchRunner(
        dataset,
        now=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )
    candidate = runner.freeze(Adapter())
    result = runner.evaluate(candidate, Adapter())

    assert seen["development"] == (
        roots[ResearchSplit.TRAIN],
        roots[ResearchSplit.VALIDATION],
    )
    assert seen["test"] == roots[ResearchSplit.TEST]
    assert result.candidate_sha256 == seen["candidate"]
    with pytest.raises(ResearchIsolationError, match="changed after"):
        runner.evaluate(replace(candidate, payload_json='{"parameter":"changed"}'), Adapter())


def test_research_partitions_must_be_physically_separate(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "data").write_text("same", encoding="utf-8")
    digest = directory_sha256(shared)
    with pytest.raises(ResearchIsolationError, match="roots must be isolated"):
        IsolatedResearchDataset(
            train=ResearchPartition(
                ResearchSplit.TRAIN,
                shared,
                date(2020, 1, 1),
                date(2020, 12, 31),
                digest,
            ),
            validation=ResearchPartition(
                ResearchSplit.VALIDATION,
                shared,
                date(2021, 1, 1),
                date(2021, 12, 31),
                digest,
            ),
            test=ResearchPartition(
                ResearchSplit.TEST,
                shared,
                date(2022, 1, 1),
                date(2022, 12, 31),
                digest,
            ),
        )
