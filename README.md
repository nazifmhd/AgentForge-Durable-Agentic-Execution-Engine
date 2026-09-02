# AgentForge

**A durable agentic execution engine.** Define multi-step AI workflows, run them across
crashes and retries with transactional guarantees, route each step to the cheapest capable
model, and pause for a human when confidence is low — all auditable and replayable.

> Status: **Phase 6 — HTTP API.** The engine is being built phase by phase; see
> [Roadmap](#roadmap).

---

## Why this exists

Agent frameworks handle the happy path. Production breaks it: LLM APIs time out and return
malformed JSON, workers crash mid-workflow, retried steps send the same email twice, costs
balloon because every call uses the most expensive model, and stuck workflows have no
off-ramp for a human. AgentForge treats an agent run like a database transaction — durable,
recoverable, cost-bounded, and auditable.

## How it works

- **Event-sourced core.** Every state change is an append-only event; instance state is a
  fold over its event log. Free audit trail, deterministic replay, no lost-update races.
- **Worker leasing.** Multiple workers pull instances off a Postgres queue with
  `FOR UPDATE SKIP LOCKED`, renew a lease via heartbeat, and a recovery sweep reclaims
  leases whose worker died — that *is* the crash-recovery mechanism.
- **Exactly-once side effects.** Effects go through a guard that records intent, prefers
  provider-side idempotency keys, and reconciles on recovery. Each has an optional
  compensation action for rollback.
- **Cost-aware routing.** A config-driven model registry + pre-flight token estimation
  picks the cheapest model in the task's tier that fits the budget, with fallback chains.
- **Human-in-the-loop.** Steps can require approval or escalate on low confidence / cost
  threshold / anomaly; the workflow parks in `WAITING_APPROVAL` until a human responds or a
  deadline auto-action fires.

## Quick start

```bash
# 1. infra (Postgres, Redis, OTel, Grafana)
make up

# 2. deps  (needs `uv` — https://docs.astral.sh/uv/)
make install

# 3. migrate + run
make migrate
make run-api      # http://localhost:8000/docs
make run-worker   # in another shell — claims leases, drives workflows, recovers

# 4. mint an API key (needs the DB up)
uv run agentforge apikey --tenant acme --name laptop --scopes admin
```

Then `curl -H "X-API-Key: af_…" localhost:8000/api/v1/workflows`.

No `uv`? `python -m venv .venv && .venv/bin/pip install -e ".[dev,test,agents]"`.

## Layout

```
src/agentforge/
  core/            # domain-agnostic engine: events, state machine, executor,
                   # scheduler, checkpoint, recovery, side effects, cost router,
                   # budget, escalation, dead-letter
  agents/          # base agent interfaces (planner / executor / validator / reflector)
  api/             # FastAPI surface, auth, middleware, websockets
  integrations/    # ActionProvider (native + n8n adapter), LLM providers, notifications
  observability/   # OpenTelemetry, structured logging, health
  use_cases/       # reference implementation: sales_intelligence
config/models.yaml # cost-aware model registry
migrations/        # Alembic
tests/             # unit / integration / evals
docs/adr/          # architecture decision records
```

## Roadmap

| Phase | Scope | Status |
|------:|-------|--------|
| 0 | Scaffold: tooling, CI, compose, Alembic, config | ✅ |
| 1 | Event-sourced core: domain models, DAG, event vocabulary, `fold`, snapshots, `EventStore` | ✅ |
| 2 | Step executor, worker leasing + heartbeat + fencing, workflow driver, crash recovery | ✅ |
| 3 | Side-effect guard (outbox + provider idempotency), `ActionProvider`, compensation/rollback, DLQ requeue | ✅ |
| 4 | Config-driven model registry, cost-aware router, pre-flight budget (workflow + tenant/day), LLM providers | ✅ |
| 5 | Escalation controller (resolve / skip / abort / budget-bump), deadline auto-actions, notifications, event pub/sub for streaming | ✅ |
| 6 | FastAPI surface (workflows / instances / escalations / DLQ / webhooks / WebSocket stream), API-key + JWT auth with scopes, per-tenant rate limiting, RLS | ✅ |
| 7 | LangGraph agent runtime + base agents | ⏳ |
| 8 | Sales Intelligence reference implementation | |
| 9 | Observability wiring, n8n adapter | |
| 10 | Eval framework, prod compose/k8s, docs hardening | |

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — component design and the ADR index
- [docs/adr/](docs/adr/) — decision records
- `LESSONS_LEARNED.md` — added as real problems are hit

## License

Apache-2.0
