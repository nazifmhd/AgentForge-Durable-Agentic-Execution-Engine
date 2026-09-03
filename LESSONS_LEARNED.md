# Lessons Learned

Real problems hit during the build and how they were solved. Added as they happen.

## Phase 0

- **The blueprint's `checkpoint_version` optimistic lock doesn't survive parallelism.**
  A single version integer on the instance row means every parallel step completion
  contends on it. Switched the core to event sourcing (ADR-0002) before writing any
  execution code — cheaper to decide now than to migrate later.
- **Model registry rots.** The blueprint's hardcoded `claude-sonnet-4-6` / `gpt-4o` prices
  were already stale. Moved to `config/models.yaml`, hot-reloadable, verified current IDs
  and pricing against the provider docs.

## Phase 1

- **One `checkpoint_version` per instance can't express parallel step completion.** Under
  event sourcing the guard moved to `UNIQUE(instance_id, sequence)` on the event log:
  independent step-completion events never collide, and two workers racing the *same*
  sequence number is caught by the constraint (→ `ConflictError`). Verified with a
  concurrent-append test.
- **"Replay" needs the fold to be pure and incremental.** A property test asserts
  `fold(events)` == `fold(tail, base=snapshot_of(fold(head)))` for every split point — this
  is the invariant that makes snapshots safe and replay-from-checkpoint correct.
- **Cost needs a single source of truth.** `StepCompleted` and `LLMCallRecorded` both
  *could* carry cost; summing both double-counts. Decided: only `CostCharged` moves
  `instance.cost_accumulated_usd`, and it's emitted for failed attempts too (you pay for
  those). Per-step figures are informational.
- **`fold` should reject a corrupt log, not limp along.** Illegal status transitions,
  non-contiguous sequences, and a stale `from_status` all raise rather than being silently
  applied — a bad event stream is a bug to surface, not smooth over.

## Phase 2

- **CI was red from commit 1 — no `uv.lock`.** `uv sync --frozen` needs a committed
  lockfile; there wasn't one (no `uv` on the dev box). Installed `uv` via `pip`, ran
  `uv lock`, committed it. Lesson: run the CI install command locally once before pushing a
  new toolchain.
- **"Recover from N adversarially-timed crashes" is not a fair property.** The first
  crash-recovery property test forced a crash after *every* append forever — under which no
  system makes progress (StepStarted persists → crash → recovery resets → repeat). Narrowed
  it to "one crash at a varying point, spaced so forward progress is possible", which is the
  real durability guarantee. The pathological version was testing physics, not the code.
- **Separate `StepStarted` append buys observability but needs a reset path.** Persisting
  `StepStarted` before running the attempt means a crashed worker leaves the step `RUNNING`;
  the next driver must reset `RUNNING → READY` on pickup (added that transition + a
  `_recover_in_flight` pass). The alternative — one atomic append after the runner returns —
  is simpler but blinds you to in-flight work.
- **Fencing has to be inside the append transaction.** A lease check before `append_new`
  is TOCTOU. The guard runs as a callback *within* the append's transaction, re-reading the
  lease row; combined with the `UNIQUE(instance_id, sequence)` version check, a worker that
  lost its lease cannot write even under a partition.
- **In-memory doubles were worth the cost.** Postgres isn't available on the dev box, so
  the executor / driver / worker / recovery all got `InMemoryJournal` + `InMemoryLeaseStore`
  doubles mirroring the PG semantics (version conflict, fence guard, claim gating). 63 unit
  tests run with no infra; the PG-specific SQL gets thinner integration coverage in CI.

## Phase 3

- **The blueprint's "outbox" is really a dedup table.** Its flow (write intent → execute →
  mark done, all inline) still re-fires if the process dies between execute and mark-done.
  The guard now: (1) atomic upsert-with-`RETURNING` claims the row and tells us the attempt
  count, (2) on a *resumed* attempt with a non-idempotent provider, `reconcile()` asks the
  provider "did the last try land?" before re-executing, (3) idempotent providers just get
  the same key. Guarantee is labeled per effect (`exactly_once` vs `at_least_once_dedup`).
- **Same pattern again: extract a store protocol so it's locally testable.** `OutboxStore`
  (Pg + in-memory) let the dedup/reconcile/compensate logic get 6 unit tests with no DB;
  the atomic-claim-under-concurrency check is the one part that needs real Postgres.
- **DLQ requeue is just another event.** `WorkflowRequeued` folds to
  `DEAD_LETTERED → RUNNING` + the failed step back to `READY` with `attempts = 0` (fresh
  budget — the operator is asserting the transient cause is fixed). No lease needed: a
  dead-lettered instance has no live worker.
- **Rollback = compensate effects (newest first) then steps (reverse order).** A failed
  compensation doesn't silently strand the workflow — it raises `CompensationError` and the
  driver escalates to `WAITING_APPROVAL` with a `compensation_failed` escalation.
- **Workflow-level escalations carry no step.** `EscalationRaised(step_id="")` needed a
  guard in the fold so it doesn't materialise a phantom step named `""`.

## Phase 4

- **Context-window and max-output filters bite before the budget filter.** Two router
  tests were "over budget" in intent but actually hit `NoEligibleModelError` first because
  the expected output exceeded the model's `max_output_tokens` / context window. Order the
  filters deliberately and write budget tests with an output size the model can actually
  serve.
- **A budget refusal is not a workflow bug.** `CostAwareRouter` raises `BudgetExceededError`
  *before* any paid call; the driver catches that specific `error_type` and escalates
  `cost_threshold` → `WAITING_APPROVAL` (raise the limit, resume) regardless of the
  `on_failure` policy — pausing or dead-lettering would be the wrong response.
- **`FAILED → WAITING_APPROVAL` had to be added to the step transition table** so a
  budget-refused step can be held for a human without first bouncing through `READY`.
- **SDKs go in the `agents` extra, so provider modules import them lazily.** `import agentforge`
  never needs `anthropic`/`openai`; `build_provider(name)` and each provider's `_load_sdk()`
  do the import at construction and raise a clear `ConfigurationError` if the extra's missing.
- **Tenant-daily budget needs its own ledger.** Summing `instance_index.cost_accumulated`
  by date is wrong (that's lifetime cost). Added `tenant_cost_ledger` (one row per
  tenant/day, `INSERT … ON CONFLICT DO UPDATE SET cost = cost + n`), bumped by the driver
  right after it appends the `CostCharged` events.

## Phase 5

- **Deadline timers don't need a timers table.** The `escalations` read model (rebuilt
  from events by `EventStore._commit`, like `instance_index`) already stores
  `deadline` + `auto_action`; the sweeper just queries `status='pending' AND deadline <= now`.
  Retry backoff already uses `instance_index.next_wakeup_at`, so there's nothing else timed.
- **Two more step transitions surfaced from HITL.** `WAITING_APPROVAL → READY` (an approved
  step goes back to the ready queue) and `FAILED → WAITING_APPROVAL` (a budget-refused step
  held for a human — added in Phase 4). The state machine grows one edge at a time as real
  flows need it, and each edge gets a comment saying why.
- **Same store-protocol pattern, third time.** `EscalationController` reads through an
  `EscalationReadStore` (`Pg` + a test double fed by `InMemoryJournal`'s own escalation
  projection). The controller's resolve/timeout *logic* — which events to append, which
  transitions — is fully unit-tested with no DB; only the read queries need Postgres.
- **Publishing is off the durability path.** `EventStore` takes an optional `EventPublisher`
  and calls it *after* the transaction commits. A Redis outage → `RedisEventPublisher`
  logs and swallows → no live updates, never lost or delayed state.
- **The WebSocket endpoint itself is Phase 6.** Phase 5 ships the pub/sub mechanism and a
  testable `InstanceStream` async iterator; the FastAPI route that relays it to a browser
  lands with the rest of the API surface.

## Phase 6

- **FastAPI 0.141's `_IncludedRouter` makes `len(app.routes)` misleading.** `include_router`
  now stores a lazy wrapper instead of copying routes eagerly, so route counts look wrong
  until resolved. Verify routing through `/openapi.json` or an actual request, not
  `app.routes`.
- **Ruff's `B008` fights the FastAPI DI idiom.** `Depends(...)` in an argument default is
  the whole point of FastAPI. Added `fastapi.Depends`/`Query`/`Path`/… to
  `flake8-bugbear.extend-immutable-calls`, and hoisted every `require(scope)` /
  `rate_limited(bucket)` to a module-level singleton so the inner call isn't in the default.
- **Operator control races the worker, and that's fine.** `pause`/`resume`/`abort` append
  with no lease guard; if a worker is mid-drive the version bump makes its next append
  `ConflictError` → it bails (`LEASE_LOST`) and the operator's transition stands. A short
  optimistic retry rides out the window. Two new workflow transitions fell out:
  `PENDING → PAUSED` and (Phase 5) `WAITING_APPROVAL → READY`.
- **`InstanceService` shouldn't depend on `EventStore` the class.** Widened it to the
  `EventJournal` / `DefinitionSource` protocols so the API tests can wire it to the same
  in-memory doubles the engine tests use — the whole HTTP surface gets 9 tests with no DB.
- **RLS is real but the test fixture uses `create_all`.** The `0006` migration enables
  row-level security (permissive when the `agentforge.tenant_id` GUC is unset, so the
  worker still sees everything); the integration test sets the GUC and asserts a WHERE-less
  query is clamped — skipping when the policy isn't present (local fixture) and running in
  CI where migrations apply first. Primary isolation is still the repository layer's
  `tenant_id` on every query.

## Phase 7

- **`StateGraph(dict)` replaces the whole state; a `TypedDict` schema merges per key.**
  With an untyped `dict` schema, each node's returned dict becomes the entire new state —
  `goal` and the threaded `ctx` vanished after the first node. Every agent now declares a
  `TypedDict` state (subclassing `AgentState`, which carries `ctx`), so partial node updates
  merge channel-by-channel and concurrent `.run()` calls never share state.
- **The LangGraph checkpointer stays off (ADR-0006).** An agent's internal graph runs to
  completion inside one step attempt; AgentForge owns durability at the step boundary. The
  `StepContext` rides through graph state, not on the agent object, so one `PlannerAgent`
  instance drives many steps concurrently.
- **`ctx.llm` is the only way agents reach a model.** `ask_text` / `ask_json` wrap it, so
  every agent call is cost-routed, budget-checked and auto-charged. `ask_json` tolerates
  ```json fences and does one repair round before raising `MalformedOutputError`.
- **The executor loop needs a hard cap, not just a model that says "final".** `_route`
  checks `iterations < _MAX_TOOL_CALLS` independently of the decision, so a model that keeps
  asking for tools still terminates and gets one synthesised final answer.
- **`langgraph` is an optional extra.** `_load_langgraph()` imports lazily and raises
  `ConfigurationError` with an install hint; `agents/**` is PLC0415-exempt like the other
  lazy-import modules.

## Phase 8

- **The DAG has no conditional edges — branching lives in the agents.** The driver
  runs every step of a definition; there is no "skip the rest" signal. The
  disqualified-lead path is modelled by the `draft_outreach` and `send` agents
  reading the upstream `score.tier` and short-circuiting to a `{"skipped": true}`
  output *before any LLM call or side effect*. The workflow still COMPLETES with
  all four steps; three of them just did nothing expensive.
- **Flatten step outputs that the next step consumes directly.** `_resolve_inputs`
  puts each dependency's output under its `step_id` key. The scoring step returns
  the `LeadScore` dict *as* its output (not wrapped in `{"score": ...}`), so a
  downstream `ctx.inputs["score"]["tier"]` reads cleanly instead of
  `["score"]["score"]["tier"]`.
- **A step that needs a non-adjacent ancestor's output must depend on it.**
  `draft_outreach` needs the dossier (from `research`) and the score (from
  `score`), so it lists both as `dependencies` even though `score` already
  depends on `research`. Dependencies are the only way data reaches a step.
- **`requires_approval` is a step-definition flag, not agent logic.** The send
  agent has no idea approval happened; the driver escalates before dispatching
  the step and only runs it once the escalation resolves `approve`. Keeps the
  agent testable in isolation.
- **The ICP tier decision is code, not model output.** The LLM returns a fit
  score and cited evidence; `_calibrate` clamps it and applies the YAML
  thresholds + disqualifier check. The model can be wrong about a number; it
  can't silently flip a qualify/disqualify decision.
- **Shared test double: `ScriptedLLMProvider`.** Multi-node agent graphs make one
  LLM call per node; a provider that returns canned text per call, in order, is
  the natural way to drive them. Lives in `tests/doubles.py` now, used by both
  the agent-runtime and sales-intelligence suites.

## Phase 9

- **OpenTelemetry lets you set the `TracerProvider` exactly once.** A second
  `trace.set_tracer_provider(...)` logs "Overriding ... is not allowed" and is
  ignored — which silently broke a per-test fixture that recreated the provider.
  `configure_tracing()` now installs the SDK provider only if the current one
  isn't already an SDK `TracerProvider`, and otherwise *only adds span
  processors*. That is also the right production shape: idempotent, and a second
  call from a different entrypoint just no-ops.
- **`core` gets a metrics seam, not a metrics dependency.** `observability/metrics.py`
  owns the `prometheus_client` objects and exposes `record_step` / `record_llm` /
  `record_side_effect` / … . `core` calls those. The helpers are unconditionally
  safe to call, so a worker (no HTTP, nothing scraping) still accumulates them and
  a `AGENTFORGE_WORKER_METRICS_PORT` turns on its own `/metrics` with no code
  change.
- **Metric singletons make tests delta-based.** Prometheus metric objects live
  for the process; the suite runs many tests against the same counters. Every
  assertion reads the sample before and after and checks the difference.
- **`SpanProcessor` is exported from `opentelemetry.sdk.trace`, not
  `...trace.export`.** mypy's `attr-defined` caught the wrong import; the base
  class lives one module up from `BatchSpanProcessor`.
- **n8n is just an `ActionProvider` (ADR-0007 paying off).** `N8nActionProvider`
  maps `effect_name` → `{base}/webhook/{effect}` with the idempotency key as a
  header, and optional `/status` + `/compensate` companion webhooks for
  `reconcile` / `compensate`. `bootstrap` registers it when
  `AGENTFORGE_N8N_BASE_URL` is set; nothing in `core` changed.

## Phase 10

- **The eval harness reuses `StepContext` + the real `LLMClient`.** An eval case
  is just `inputs` → `StepContext` → `runner.run()` → score the output dict. No
  parallel "eval-only" agent path to drift out of sync; a scorer sees exactly
  what a production step would produce, and cost/token roll-ups come straight off
  `ctx.charges`.
- **Partial credit, hard case verdict.** A case passes only if every check
  passes, but the suite `pass_rate` is the mean of each case's *weighted score* —
  so a suite that regresses from "all checks green" to "most checks green" moves
  the number instead of flipping a single boolean. The CLI exits on
  `pass_rate >= threshold`.
- **`llm_judge` degrades soft.** No judge `LLMClient` → the check fails with a
  clear detail string rather than raising, so a suite that mixes deterministic
  and model-graded checks still runs (and reports) without a key for a quick
  structural pass.
- **One image, `CMD` picks the role.** `api` vs `worker` is a command arg, not a
  separate build. The compose `migrate` service / k8s Job gates the rollout;
  event-sourcing keeps migrations additive so a new worker and an old worker can
  run against the same schema during a rollout.
- **Workers need no leader election in k8s.** Lease coordination (ADR-0004) means
  `replicas: N` just works; a rolling update is safe mid-workflow because an
  evicted worker's leases expire and another reclaims the instance from its event
  log. Set `terminationGracePeriodSeconds` ≥ the lease length.
