# AgentForge Architecture

## System layers

```
Clients (React UI · REST · n8n webhooks · CLI)
        │
API Gateway (FastAPI)   auth · rate limit · request validation · WebSocket
        │
Orchestration
  ├─ Workflow Engine (core, domain-agnostic)
  │    event store · state machine · scheduler (DAG + leasing) · executor
  │    checkpoint/recovery · side-effect guard · cost router · budget
  │    escalation controller · dead-letter
  └─ Agent Runtime (LangGraph)   planner · executor · validator · reflector
        │
Infrastructure
  PostgreSQL (+pgvector) · Redis · Object store · OTel Collector · Prometheus · Grafana
  n8n (optional, via ActionProvider adapter)
```

## Core execution model — event sourcing

An instance's authoritative state is **not** a mutable row; it is the ordered fold of its
`workflow_events`. Writing an event is an `INSERT` — it never conflicts with a concurrent
step's event, which removes the lost-update problem the single-`checkpoint_version` design
has under `max_concurrent_steps > 1`.

Event families (Phase 1 finalizes the list):

- lifecycle: `InstanceCreated`, `InstanceStatusChanged`, `InstanceCompleted`, `InstanceFailed`
- step: `StepScheduled`, `StepStarted`, `StepCompleted`, `StepFailed`, `StepRetryScheduled`,
  `StepSkipped`, `StepCompensated`
- durability inputs: `LLMCallRecorded`, `ToolCallRecorded` (record the *result* so replay
  folds it instead of re-calling paid APIs)
- side effects: `SideEffectIntentRecorded`, `SideEffectExecuted`, `SideEffectCompensated`
- HITL: `EscalationRaised`, `EscalationResolved`, `EscalationTimedOut`
- cost: `CostCharged`

A periodic **snapshot** (`instance_snapshots`) caps replay cost: load latest snapshot, then
fold only the tail.

## Durability & recovery

- Work is claimed from a Postgres-backed queue with `SELECT … FOR UPDATE SKIP LOCKED`.
- The claiming worker holds a **lease** (`instance_leases`: instance_id, worker_id,
  expires_at) refreshed by a heartbeat every `lease_heartbeat_seconds`.
- Every mutating DB write asserts lease ownership; losing the lease raises `LeaseLostError`
  and the worker abandons the instance.
- A **recovery sweep** finds instances whose lease `expires_at` has passed, verifies via the
  event log which step was in flight, and re-enqueues from the last durable event.
- Because effects are guarded and non-deterministic inputs are recorded, re-execution after
  a crash is safe.

## Exactly-once side effects

`write intent (same txn as the triggering event)` → `execute, preferring a provider-side
idempotency key` → `record SideEffectExecuted`. On recovery, an unresolved intent is
reconciled: query the provider by idempotency key, or run the registered reconciler. Only
call it "exactly-once" for providers that support idempotency keys; others are
"at-least-once with dedup" and that is documented per effect.

## Cost & budget

- `config/models.yaml` is the registry (ids, per-MTok price, context window, capabilities,
  tier eligibility, fallback chains). No model ids hardcoded in Python.
- Routing: estimate input tokens (`count_tokens`) → filter models by tier + capability +
  context fit → pick cheapest projected total cost meeting the reliability floor.
- Budget is enforced **pre-flight**: if projected step cost > remaining workflow budget (or
  org daily budget), the step escalates instead of running.

## Agent runtime

- An agent is a LangGraph `StateGraph` wrapped as a `StepRunner` by `BaseAgent`. The graph
  runs to completion inside one step attempt; LangGraph's checkpointer is **off** —
  AgentForge owns durability at the step boundary (ADR-0006).
- Graph state is a per-agent `TypedDict` (subclassing `AgentState`). The per-run
  `StepContext` travels as the `ctx` channel, so node updates merge and concurrent step
  attempts never share state — one agent instance serves the whole fleet.
- Agents reach models only through `ctx.llm` (via `ask_text` / `ask_json`), so every call is
  cost-routed, budget-checked, and auto-charged. Side effects go through `ctx.execute_effect`
  (or an `EffectTool`), never a raw client.
- Base agents: `PlannerAgent` (analyze → draft → critique → finalize), `ExecutorAgent`
  (decide ↔ act loop with a hard tool-call cap), `ValidatorAgent` (structure → content →
  verdict + score), `ReflectorAgent` (diagnose → revise). Registered into a `StepRegistry`
  by `register_base_agents`.

## Multi-tenancy

Every table carries `tenant_id`; every query is tenant-scoped at the repository layer.
API auth resolves a principal → tenant. Budgets, rate limits, and dashboards are per-tenant.

## Integrations are pluggable

`core` never imports `httpx` for external actions. It depends on an `ActionProvider`
protocol; implementations (`NativeActionProvider`, `N8nActionProvider`) live in
`integrations/`. Same pattern for `LLMProvider` (Anthropic, OpenAI) and `Notifier`.

## ADR index

| ADR | Decision |
|----:|----------|
| [0001](docs/adr/0001-postgres-checkpointing.md) | PostgreSQL for durable state, not Redis |
| [0002](docs/adr/0002-event-sourcing.md) | Event-sourced core over mutable-row + version |
| [0003](docs/adr/0003-outbox-and-idempotency.md) | Guarded effects + provider idempotency over 2PC/saga |
| [0004](docs/adr/0004-worker-leasing.md) | Postgres queue + leases + heartbeat for recovery |
| [0005](docs/adr/0005-deterministic-replay.md) | Record non-deterministic inputs for replay |
| [0006](docs/adr/0006-langgraph-agent-runtime.md) | LangGraph for the agent runtime |
| [0007](docs/adr/0007-pluggable-action-providers.md) | n8n as one ActionProvider, not a core dependency |
| [0008](docs/adr/0008-cost-aware-routing.md) | Config-driven cost-aware routing + pre-flight budget |
| [0009](docs/adr/0009-optimistic-concurrency.md) | Optimistic concurrency at instance-level transitions |
| [0010](docs/adr/0010-multi-tenancy.md) | Row-level multi-tenancy from day one |
