# ADR-0001: PostgreSQL for durable state, not Redis

- **Status:** accepted
- **Date:** 2026-08-29

## Context

Workflow state must survive process and machine crashes, support multi-row atomic writes
(event + lease + cost in one transaction), and serve as a queryable audit trail for months.

## Decision

PostgreSQL is the system of record for all workflow state (events, snapshots, leases,
side-effect intents, escalations, dead letters). Redis is used only for ephemeral concerns:
pub/sub fan-out to WebSocket clients and short-TTL rate-limit counters.

## Consequences

- ACID transactions let us write an event and its side-effect intent atomically.
- `pgvector` is available in the same store for agent memory/RAG without another system.
- Postgres becomes the throughput ceiling; mitigated by snapshots, partitioning
  `workflow_events` by month, and read replicas for dashboards.
- Redis being non-authoritative means a Redis outage degrades (no live updates) but never
  loses or corrupts state.

## Alternatives considered

- **Redis as primary** — no real transactions across keys, persistence is best-effort
  (RDB/AOF windows), weak for audit queries.
- **Event log in Kafka** — excellent for the log, but then state queries need a separate
  materialized store; too much infra for this scale.
