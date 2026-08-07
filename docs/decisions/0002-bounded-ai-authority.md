# ADR-0002: Bound AI Authority And Preserve Attribution

- Status: Accepted
- Date: 2026-08-07
- Supersedes: None
- Superseded by: None

## Context

AI should help strategies respond to changing market conditions, but unconstrained
prompt-driven trading is not reproducible and can turn recent performance into
online overfitting. Manual execution also introduces a separate source of
performance variation.

## Decision

Use AI in three roles:

1. Offline strategy research against physically isolated train, validation, and
   test data.
2. Daily proposals for bounded weight changes among approved strategies and for
   risk reduction.
3. Structured explanation, challenge, and post-trade review.

AI cannot create fills, enable an unapproved strategy, increase configured risk
limits, or bypass the deterministic risk engine. Preserve three portfolio
tracks: deterministic base simulation, AI-adjusted simulation, and the actual
portfolio derived from manually recorded fills.

## Rationale

The boundary permits flexible analysis while keeping orders deterministic,
auditable, and attributable. Three portfolios distinguish strategy quality, AI
overlay value, and human execution effects.

## Alternatives Considered

- Let the model emit direct buy and sell orders: rejected because it is
  stochastic, difficult to backtest, and can bypass portfolio constraints.
- Use four-model majority voting: rejected because model agreement is not
  independent market evidence and obscures responsibility.
- Keep only the manually mirrored portfolio: rejected because skipped or delayed
  trades would make strategy and execution performance inseparable.

## Consequences

- AI responses require strict schemas, bounds, versions, and immutable audit
  records.
- AI allocation begins in shadow mode and needs forward evidence before any
  authority expansion.
- The UI must display base intent, AI proposal, approved target, and actual fill
  as distinct states.
