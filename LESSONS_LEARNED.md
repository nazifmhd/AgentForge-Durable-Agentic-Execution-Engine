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
