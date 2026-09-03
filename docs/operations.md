# Operations runbook

## Topology

- **API** (`agentforge api`) — stateless, horizontally scalable, behind a load
  balancer. Serves REST + the WebSocket stream, exposes `/metrics`,
  `/health/live`, `/health/ready`.
- **Worker** (`agentforge worker`) — claims instance leases from Postgres, drives
  them, heartbeats, and runs the recovery + escalation sweeps. Scale replicas
  freely; they coordinate only through Postgres (`FOR UPDATE SKIP LOCKED` +
  fenced leases, ADR-0004). No leader election.
- **Postgres** — the single source of truth (event log, read models, outbox,
  leases, ledgers). **Redis** — pub/sub for the live stream and the rate-limit
  window; the engine degrades to "no live updates" if it is down, never loses
  state.

## Deploy

Migrations run to `head` before any new code serves traffic — the
`agentforge-migrate` Job (k8s) or the `migrate` service (compose) gates the
rollout. Event-sourcing keeps schema changes additive: new event types are
forward-compatible, `fold` ignores unknown fields.

Rolling a worker is safe mid-workflow: a killed worker's leases expire and
another reclaims the instance from its event log. Give pods a
`terminationGracePeriodSeconds` ≥ the lease length so in-flight drives finish.

## What to watch (Grafana: *AgentForge — Overview*)

| Signal | Metric | Act when |
|---|---|---|
| Throughput | `rate(agentforge_workflow_drives_total[5m])` by `result` | `lease_lost` climbing → workers overloaded or clock skew |
| Step latency | `agentforge_step_duration_seconds` p95 by `agent_type` | a tier is slow → check model routing / provider health |
| Failures | `agentforge_steps_total{outcome="failure"}` fraction | sustained → inspect DLQ and recent deploys |
| LLM spend | `rate(agentforge_llm_cost_usd_total[1h])` by `model` | above forecast → tighten tiers or budgets |
| Escalations | `agentforge_escalations_total` by `reason` | `cost_threshold` spike → a workflow's budget is too low |
| Dead letters | `increase(agentforge_dead_letters_total[24h])` | any → triage below |

Every log line carries the `trace_id`; pivot from a slow span in Tempo/Jaeger to
its logs and back.

## Common tasks

**Triage the DLQ.** `GET /api/v1/dead-letters` lists parked instances with the
failing step and error. Fix the cause, then `POST /api/v1/dead-letters/{id}/requeue`
— it appends `WorkflowRequeued` and the next worker resumes from the last good
event.

**A workflow is stuck in `WAITING_APPROVAL`.** `GET /api/v1/escalations` →
resolve with `approve` / `skip` / `abort`, or `approve` + `new_budget_usd` to
bump a `cost_threshold` hold. A deadline auto-action fires on its own if
configured on the step.

**Costs running hot.** Lower `AGENTFORGE_ORG_DAILY_BUDGET_USD` (tenant/day) or a
workflow's `budget_limit_usd`; edit `config/models.yaml` tiers and send the
worker a reload (the registry is hot-reloadable). Pre-flight budgeting means an
over-budget step escalates instead of spending.

**Replay / audit an instance.** `GET /api/v1/instances/{id}/events` is the full
event log; `?at_version=N` returns the folded state as of any point.

**Scale for a backlog.** Add worker replicas and/or raise
`AGENTFORGE_MAX_CONCURRENT_STEPS_PER_WORKER`. The claim query is already
contention-free; Postgres connections are the usual ceiling.
