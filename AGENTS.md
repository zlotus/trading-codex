# Repository Instructions

## Project Context

For non-trivial work or when resuming development, read `docs/context.md` and
`docs/progress.md`, then only the task-relevant ADRs linked from
`docs/decisions/README.md`. Read `docs/ideas.md` only during product planning.

Treat repository instructions and external specifications as normative, code,
tests, and configuration as the implemented behavior, accepted ADRs as durable
rationale, and `docs/progress.md` as a current handoff snapshot. If these
sources disagree, verify the behavior and update stale documentation when it is
in scope.

Keep `docs/context.md` stable and `docs/progress.md` concise. Record durable
architecture, security, protocol, dependency, data, or product decisions as
ADRs. Preserve accepted decisions; add a replacement ADR and mark the old one
as superseded when a decision changes.

Do not update project-context documentation for routine edits, formatting,
small isolated fixes, or unaccepted exploration. Record only checks actually
run. Do not commit or push unless the user explicitly requests it.

## Engineering Rules

- Keep strategy and allocation logic independent from backtest and live-data
  frameworks.
- Every market-data query used for a decision must enforce an explicit `as_of`
  boundary.
- AI output is a proposal. It cannot bypass deterministic risk checks or create
  an executable fill.
- Preserve the baseline, AI-adjusted, and manually executed portfolio tracks so
  their effects remain attributable.
- Fail closed when required data is stale, missing, or internally inconsistent.
- Keep the application a local, single-user modular monolith unless an accepted
  ADR changes that constraint.
