# ADR-0008: Config-driven cost-aware routing + pre-flight budget

- **Status:** accepted
- **Date:** 2026-08-29

## Context

Fixed model assignment wastes money on simple steps. The blueprint hardcodes a model
registry in Python with prices and ids that change monthly, and enforces budget only
*after* a step has already overspent.

## Decision

- The registry is `config/models.yaml` (ids, per-MTok in/out price, context window,
  capabilities, tier eligibility, ordered fallback chains). Hot-reloadable; no ids in code.
- Routing per call: estimate input tokens with the provider's `count_tokens` → filter by
  required tier, capabilities (tools/vision), context fit, reliability floor → choose the
  lowest projected total cost. Retryable failures walk the tier's fallback chain.
- **Pre-flight budget check:** if projected step cost > remaining workflow budget OR the
  tenant's remaining daily budget, the step raises `BudgetExceededError` → escalates
  (`COST_THRESHOLD`) instead of running.
- Actual `CostCharged` events feed a rolling per-`(task_type, model)` quality/'success'
  signal that can bias future routing.

## Consequences

- Model/price changes are a config edit and a deploy of the file, not a code change.
- Budgets are never silently blown.
- `count_tokens` adds one cheap pre-call round trip; acceptable, and it doubles as a
  context-window guard.

## Alternatives considered

- **Hardcoded registry (blueprint)** — stale within weeks.
- **Post-hoc budget only** — the overspend already happened.
