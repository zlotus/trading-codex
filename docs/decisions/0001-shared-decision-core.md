# ADR-0001: Shared Decision Core With Replaceable Execution Adapters

- Status: Accepted
- Date: 2026-08-07
- Supersedes: None
- Superseded by: None

## Context

The system must use identical strategy semantics in historical replay and daily
decisions while modeling A-share execution rules. Existing frameworks reduce
accounting risk but can impose data formats, strategy APIs, runtime constraints,
or licenses that should not control the rest of the application.

The product is personal and single-user. Operational simplicity and auditability
matter more than horizontal scaling.

## Decision

Build a local modular monolith around a framework-independent Python decision
pipeline. Strategy, regime, allocation, and risk logic depend only on project
domain contracts and a point-in-time market-data view.

Use RQAlpha as the first execution-backtest adapter candidate. Confirm it with a
bounded data and accounting spike before adoption. If it fails, implement only
the narrow event simulator required for daily and 09:35 decisions behind the
same adapter contract. Reserve Qlib for later research rather than transaction
state or live decisions.

## Rationale

This keeps one authoritative decision path without accepting the cost and risk
of writing a general-purpose backtest platform. It also confines external
framework behavior to a testable boundary.

## Alternatives Considered

- Write strategies directly against RQAlpha: rejected because live decisions
  and framework replacement would duplicate or rewrite strategy logic.
- Build a full custom backtester immediately: rejected because calendars,
  accounting, corporate actions, and fills carry a high correctness burden.
- Use microservices: rejected because a personal local application has no
  concurrency requirement that justifies distributed operations.
- Use Qlib as the application core: rejected because its research workflow does
  not replace the manual-fill ledger and daily operational workspace.

## Consequences

- Domain contracts and causality tests must be defined before strategy growth.
- The RQAlpha adapter may require substantial BaoStock normalization work.
- Backtest execution and live manual execution remain different adapters but
  produce the same order-intent and ledger contracts.
- Framework-specific dependencies can run in a separate pinned environment.
