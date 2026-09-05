# ADR-0005: Record non-deterministic inputs for deterministic replay

- **Status:** accepted
- **Date:** 2026-08-29

## Context

The API exposes `POST /instances/{id}/replay` but the blueprint has no mechanism. Naive
replay re-executes steps, which re-calls paid LLM/tool APIs and re-fires side effects — not
a replay, a re-run with new costs and new outcomes.

## Decision

Every non-deterministic input a step consumes is captured as an event at first execution:

- `LLMCallRecorded` — request digest + full response + token usage + model
- `ToolCallRecorded` — tool name + args digest + result
- wall-clock reads go through an injected `Clock`; the observed time is recorded
- randomness goes through an injected seeded RNG; the seed is recorded

Replay runs the same step code with a **replaying** LLM/tool/clock/RNG that returns the
recorded values in order and asserts the request digests match (drift = the code changed;
surfaced, not silently ignored). Side effects are **not** re-executed during replay.

## Consequences

- Replay is free (no API spend) and faithful.
- Enables "what if" debugging: replay to step N, then branch with live execution.
- Step functions must route all I/O and time/random through injected ports — enforced by
  code review and a lint rule banning `datetime.now`/`random` in `core` and agent modules.
- Recorded LLM responses can be large; stored compressed, and snapshots let us prune very
  old event bodies while keeping digests.

## Implementation status

Built: `LLMCallRecorded` capture through the single `StepContext.llm` chokepoint,
and **replay on recovery** — when a crashed step re-runs, its recorded responses
are replayed in order (provider not called, nothing re-charged); a retry after a
`StepFailed` clears the recordings so the next attempt calls fresh. This is the
part the blueprint critique was about ("replay just re-runs and re-bills").

Deferred: a standalone `POST /instances/{id}/replay` for offline "replay to step
N then branch" debugging, RNG seed recording, and `ToolCallRecorded` emission
(read-only tools are called outside `ctx`; side-effecting ones already go through
the exactly-once guard). `request_digest` is recorded but not yet asserted on
replay.

## Alternatives considered

- **No replay** — drop the feature. Rejected: replay/audit is a headline capability.
- **Re-run with a cost cap** — still non-faithful and still costs money.
