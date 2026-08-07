# Implementation Plan

The plan is ordered to validate data and accounting correctness before adding
strategy complexity or AI authority.

## Milestone 0: Application Scaffold

Deliverables:

- Repository-owned context, ADRs, and acceptance-oriented plan.
- Minimal FastAPI service with health and component-status endpoints.
- Responsive React shell for the daily decision workspace and AI side panel.
- Python package boundaries for data, strategy, regime, risk, backtest, AI, and
  portfolio modules.

Acceptance:

- Backend tests and lint pass.
- Frontend production build passes.
- API and web development servers start locally.

## Milestone 1: Data Foundation And Backtest Spike

Deliverables:

- BaoStock instrument, calendar, daily-bar, adjustment, suspension, ST, and
  historical-universe ingestion.
- Immutable raw storage, normalized Parquet datasets, and data-quality reports.
- Historical opening/5-minute coverage assessment for the 09:35 checkpoint.
- RQAlpha adapter spike over 20 instruments and representative edge-case dates.

Acceptance:

- Incremental synchronization is idempotent.
- Every normalized record has source and receive-time provenance.
- Point-in-time tests reject future rows.
- T+1, lot size, fees, suspension, limit-up/down, and one corporate-action case
  match independently calculated fixtures.
- A documented go/no-go decision selects RQAlpha or the narrow custom simulator.

## Milestone 2: Shared Decision Kernel

Deliverables:

- Versioned feature pipeline and end-of-day candidate shortlist.
- First deterministic strategy: volatility-scaled cross-sectional momentum.
- Target allocator, hard risk engine, and execution planner.
- Historical replay using the same pipeline intended for live decisions.

Acceptance:

- Full-history and truncated-history causality checks produce identical signals
  at sampled timestamps.
- Replaying an unchanged decision snapshot produces the same base target.
- No trade is proposed from stale data or against A-share execution constraints.

## Milestone 3: Ledger And Daily Vertical Slice

Deliverables:

- Append-only signals, order intents, fills, cash movements, and position views.
- Base, AI-shadow, and actual portfolio identifiers over the same ledger model.
- End-of-day preparation and 09:35 decision jobs with retryable run records.
- Working decision table, signal detail chart, fill entry, and reconciliation UI.

Acceptance:

- Partial fills, skipped signals, fees, and T+1 available quantities reconcile.
- Accounting invariants hold after every ledger event.
- A decision can be followed from source snapshot to actual fill or expiry.

## Milestone 4: Regime-Aware Strategy Pool

Deliverables:

- Interpretable market-regime probabilities from trend, volatility, breadth,
  turnover, concentration, and opening-session features.
- Momentum, short-horizon reversal, defensive low-volatility, and cash policies.
- Bounded allocator with hysteresis, turnover limits, and emergency risk-off.
- Walk-forward and regime-sliced evaluation reports.

Acceptance:

- Strategy changes occur only at configured checkpoints.
- Regime and allocator versions are present in every decision run.
- Performance is reported net of costs with parameter sensitivity, drawdown,
  alpha/beta, block-bootstrap, and deflated-Sharpe evidence.

## Milestone 5: AI Research And Shadow Allocation

Deliverables:

- Provider-neutral LLM client with structured output, prompt versions, budgets,
  timeouts, caching, and audit records.
- Offline research harness with physically isolated test data.
- Daily AI proposal limited to approved strategy-weight deltas and risk
  reduction; deterministic fallback is no change.
- Structured AI summary, proposal, evidence, and dialogue views.

Acceptance:

- Invalid, late, unknown-strategy, or out-of-bound proposals are rejected.
- AI has no direct write path to fills or risk configuration.
- Base and AI-shadow results remain independently measurable.

## Milestone 6: Forward Paper Operation

Deliverables:

- Scheduled daily operation, provider health, alerts, backup, and replay tools.
- At least 60 trading days of base and AI-shadow observations.
- Review report separating strategy, AI overlay, simulated execution, and manual
  execution effects.

Acceptance:

- Data outages and model outages fail closed without corrupting portfolio state.
- Every observed discrepancy has a reproducible decision and ledger trail.
- Any increase in AI authority requires a new accepted ADR.

## Deferred

- Qlib machine-learning factor models.
- News and filing ingestion for point-in-time AI context.
- Paid market-data providers and automated broker gateways.
- Intraday strategies below the 5-minute decision horizon.
