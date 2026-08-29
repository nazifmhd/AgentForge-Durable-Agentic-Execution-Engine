# ADR-0004: Postgres queue + leases + heartbeat for crash recovery

- **Status:** accepted
- **Date:** 2026-08-29

## Context

The blueprint says workflows "resume from the last checkpoint" but never says how a fleet of
workers claims work without two workers running the same instance, or how a crashed worker's
work is noticed and picked up.

## Decision

- **Claim:** workers poll a runnable-instance query with `SELECT … FOR UPDATE SKIP LOCKED`,
  claiming a batch atomically.
- **Lease:** the claim inserts/updates `instance_leases(instance_id, worker_id, expires_at)`.
  Ownership is asserted on every mutating write for that instance.
- **Heartbeat:** the worker extends `expires_at` every `lease_heartbeat_seconds`
  (default 10s; lease 30s → tolerates two missed beats).
- **Recovery sweep:** a background task re-enqueues instances whose lease has expired, after
  consulting the event log to find the resume point.

No separate broker (RabbitMQ/SQS) — Postgres already holds the state and gives us
`SKIP LOCKED`.

## Consequences

- Recovery is automatic and needs no operator action.
- At-most-one active worker per instance under normal operation; a network partition can
  briefly yield two, which the lease-ownership assertion on writes catches
  (`LeaseLostError`).
- Polling adds load; tuned via batch size and adaptive poll interval. Redis Streams
  consumer groups remain a future option if Postgres polling becomes the bottleneck.

## Alternatives considered

- **External queue** — another system to run and reason about; state/queue split invites
  inconsistency.
- **Advisory locks only** — no visibility into who holds what, no expiry.
