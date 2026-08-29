# ADR-0002: Event-sourced core over mutable-row + version

- **Status:** accepted
- **Date:** 2026-08-29

## Context

The original blueprint mutates a `workflow_instances` row and guards it with one
`checkpoint_version` integer under optimistic locking. With `max_concurrent_steps > 1`,
several steps finish near-simultaneously and every one of them tries to bump the same
version — they serialize into a retry storm, and a lost update silently drops a step
result. Replay and audit ("what exactly happened, in order?") are also bolted on.

## Decision

The authoritative state of a workflow instance is the ordered fold of its append-only
`workflow_events`. State transitions are expressed as events; the in-memory
`WorkflowInstance` is a projection rebuilt by `fold(events)`. Periodic `instance_snapshots`
bound replay cost.

## Consequences

- Concurrent step completions append independent rows — no write contention, no lost
  updates.
- Audit trail and time-travel debugging are inherent, not extra tables.
- `replay` becomes "re-fold from event N" rather than re-running logic.
- Cost: every read either folds events or loads a snapshot + tail; projections must be kept
  pure and versioned. We accept a small read-latency increase for correctness.
- Schema migrations on events are append-only (new event types, never mutate old ones);
  upcasters handle old shapes.

## Alternatives considered

- **Blueprint's mutable row + version** — simple, but the concurrency and replay problems
  above are disqualifying for a "durable execution engine".
- **Temporal / DBOS** — proven, but "the engine is the product" here; we adopt their
  event-history model rather than the dependency.
