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
  ├─ Agent Runtime (LangGraph)   planner · executor · validator · reflector
  └─ Use cases (on top of the engine)   Sales Intelligence & Outreach
        │
Infrastructure
  PostgreSQL (+pgvector) · Redis · Object store · OTel Collector · Prometheus · Grafana
  n8n (optional, via ActionProvider adapter)
```

Offline: `agentforge.evals` scores agents against YAML datasets (`agentforge eval`).

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

## Reference implementation — Sales Intelligence & Outreach

`use_cases/sales_intelligence/` is a full workflow built the way a real one would
be, and the engine's acceptance test:

    research ──▶ score ──▶ draft_outreach ──▶ send

- **research** — a LangGraph agent that calls read-only tools (`crm_lookup`,
  `web_enrich` — injectable, inert by default) then an LLM to assemble a
  `ResearchDossier`.
- **score** — the LLM proposes a 0-100 fit score with evidence; the **tier**
  (hot / warm / cold / disqualified) is derived in code from the `ICPProfile`
  thresholds (`config/sales_intelligence.yaml`), so qualify/disqualify is
  auditable. A `disqualified` tier makes the next two steps no-ops — **no LLM
  spend, no side effects** — showing the cost-efficiency pillar.
- **draft_outreach** — writes and self-reviews the email + LinkedIn copy against
  a house style, with one revision pass if the reviewer flags issues.
- **send** — creates a CRM task and enqueues the email through
  `execute_effect` (outbox + provider idempotency key), so a crash or retry never
  double-sends. `requires_approval` by default (HITL); `on_failure=ROLLBACK`
  cancels the CRM task and recalls the email if the send fails.

The two side effects go through an `ActionProvider` (`InMemorySalesProvider` for
tests / demo, `WebhookSalesProvider` for a real CRM+ESP), never a client in the
agent.

## Multi-tenancy

Every table carries `tenant_id`; every query is tenant-scoped at the repository layer.
API auth resolves a principal → tenant. Budgets, rate limits, and dashboards are per-tenant.

## Integrations are pluggable

`core` never imports `httpx` for external actions. It depends on an `ActionProvider`
protocol; implementations (`NoopActionProvider`, `HttpActionProvider`,
`N8nActionProvider`) live in `integrations/`. `N8nActionProvider` maps each effect
to an n8n webhook workflow (`{base}/webhook/{effect}`), passing the idempotency key
as a header and optionally driving companion `/status` (reconcile) and
`/compensate` webhooks. `bootstrap` registers it automatically when
`AGENTFORGE_N8N_BASE_URL` is set. Same pattern for `LLMProvider` (Anthropic,
OpenAI) and `Notifier`.

## Observability

`configure_observability()` (every entrypoint calls it) wires three signals:
JSON logs with the active `trace_id` bound on every line; Prometheus metrics on
the default registry (`agentforge_steps_total`, `agentforge_step_duration_seconds`,
`agentforge_llm_cost_usd_total`, `agentforge_side_effects_total`,
`agentforge_escalations_total`, `agentforge_workflow_drives_total`, …), scraped at
`/metrics` on the API and, when `AGENTFORGE_WORKER_METRICS_PORT` is set, on the
worker; and OTLP spans (`workflow.drive` → `workflow.step` → `llm.complete`, plus
auto-instrumented FastAPI) exported when `AGENTFORGE_OTEL_EXPORTER_OTLP_ENDPOINT`
is set. See [ADR-0011](docs/adr/0011-observability.md).

## Evals

`agentforge.evals` is an offline harness (no engine, no DB): a YAML *suite* names
a target `agent_type` and a list of cases; `EvalRunner` runs the agent per case
with the real cost-routed `LLMClient` and scores the output with named scorers
(`equals`, `one_of`, `in_range`, `json_keys`, `contains`, `regex`, `non_empty`,
and `llm_judge` for a model-graded rubric). `agentforge eval <suite>` prints a
per-check report and exits non-zero below the suite threshold, so agent quality
is a CI gate. See [docs/evals.md](docs/evals.md).

## Deployment

One image, two roles (`api` / `worker`); `agentforge-migrate` runs Alembic to
head before new code serves. Workers are lease-coordinated (ADR-0004) so they
scale by replica count with no leader election and roll safely mid-workflow.
`docker-compose.prod.yml` and the Kustomize base in `deploy/k8s/` are the
reference deployments; [docs/deployment.md](docs/deployment.md) and
[docs/operations.md](docs/operations.md) cover the rest.

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
| [0011](docs/adr/0011-observability.md) | structlog + Prometheus + OTLP traces, one `configure_observability()` |
