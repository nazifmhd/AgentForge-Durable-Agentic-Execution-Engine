# ADR-0009: Optimistic concurrency at instance-level transitions

- **Status:** accepted
- **Date:** 2026-08-29

## Context

Even with an event-sourced core (ADR-0002), some operations are genuine instance-level
transitions that must not interleave: pause vs. resume vs. abort, "am I the worker that may
schedule the next wave of steps?", snapshot creation.

## Decision

- Step-result events: no locking needed — independent appends.
- Instance-level transitions: guarded by a monotonic `instance_version` on the snapshot
  row, checked-and-bumped with a conditional `UPDATE … WHERE instance_version = $expected`.
  A mismatch raises `ConflictError` and the caller re-folds and retries.
- Lease ownership (ADR-0004) is the coarse guard that keeps two workers from even attempting
  concurrent transitions in the common case; `instance_version` is the correctness backstop
  for partitions and for API-initiated transitions (pause/abort) racing a worker.

## Consequences

- High throughput for the common path (parallel step completion) with no contention.
- Rare, cheap retries on the narrow set of true transitions.
- Slightly more code than "lock the row", but far better concurrency.

## Alternatives considered

- **Pessimistic `SELECT FOR UPDATE` on the instance for every write** — serializes parallel
  steps, defeating `max_concurrent_steps`.
- **No concurrency control** — pause/abort can be lost or resurrected.
