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
