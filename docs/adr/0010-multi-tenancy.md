# ADR-0010: Row-level multi-tenancy from day one

- **Status:** accepted
- **Date:** 2026-08-29

## Context

The blueprint has no tenant concept. Adding isolation after tables, queries, budgets, and
dashboards exist is a large, error-prone retrofit with a real data-leak risk.

## Decision

- Every domain table has a non-null `tenant_id`.
- All data access goes through repository classes that require a `tenant_id` and inject it
  into every `WHERE`. No ad-hoc queries in services.
- Postgres Row-Level Security policies on the core tables as defense in depth
  (`current_setting('agentforge.tenant_id')`), set per connection/transaction.
- API auth resolves a principal (API key or JWT) → `tenant_id`; middleware binds it to the
  request context and the DB session.
- Budgets (`org_daily_budget`), rate limits, and Grafana dashboards are keyed by tenant.

## Consequences

- Isolation is structural, not conventional.
- Single-tenant deployments just have one tenant row — no special-casing.
- RLS adds a small per-query cost and some migration ceremony; worth it.

## Alternatives considered

- **Add later** — every phase would be rewritten, and a missed `WHERE` is a breach.
- **Database-per-tenant** — heavy at this stage; revisit for enterprise isolation tiers.
