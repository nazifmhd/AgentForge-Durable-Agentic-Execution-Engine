# ADR-0003: Guarded effects + provider idempotency over 2PC / saga framework

- **Status:** accepted
- **Date:** 2026-08-29

## Context

Side effects (send email, write CRM, call API) must not double-fire on retry or recovery.
The blueprint calls this the "outbox pattern" but executes inline: write intent → execute →
mark done. A crash between execute and mark-done re-fires on recovery — that is
at-least-once, not exactly-once.

## Decision

1. The side-effect intent is written **in the same transaction** as the event that
   triggered it (true outbox write).
2. Execution prefers a **provider-side idempotency key** (`instance_id:step_id:effect_name`)
   so a duplicate call is deduplicated by the provider (Stripe/SendGrid/etc. style).
3. For providers without idempotency keys, the effect registers a **reconciler** that the
   recovery path calls to check "did this already happen?" before re-executing.
4. Effects declare an optional **compensation action** for rollback; a failed compensation
   raises `CompensationError` and escalates to a human.

Effects are labeled `exactly_once` or `at_least_once_dedup` in their registration; the label
is surfaced in the API and docs.

## Consequences

- Honest guarantees per effect instead of a blanket claim.
- No distributed-transaction coordinator, no saga DSL to learn.
- Requires a reconciler per non-idempotent provider — real work, but localized.

## Alternatives considered

- **2PC / XA** — external SaaS APIs don't participate; non-starter.
- **Full saga framework** — heavier than needed; compensation-per-step covers our cases.
