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

## 文档语言

- 项目文档、操作指南和交接说明以简体中文为主。
- 代码标识符、命令、API 字段、协议名称和必要的上游专有名词保留原文。
- 现有英文文档在发生实质性修改时逐步翻译，避免仅为翻译产生大范围变动。

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
