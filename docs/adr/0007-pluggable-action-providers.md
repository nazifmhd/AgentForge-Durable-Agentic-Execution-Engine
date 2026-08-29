# ADR-0007: n8n as one ActionProvider, not a core dependency

- **Status:** accepted
- **Date:** 2026-08-29

## Context

The blueprint routes side effects through n8n ("n8n handles retries, auth, rate limiting").
That makes n8n a hard dependency in the critical path and adds a failure domain whose own
retry logic can double-fire effects the engine is trying to make exactly-once.

## Decision

`core` depends on an `ActionProvider` protocol:

```python
class ActionProvider(Protocol):
    async def execute(self, effect: EffectRequest) -> EffectResult: ...
    def supports_idempotency_key(self, effect_name: str) -> bool: ...
```

Implementations live in `integrations/actions/`:
- `NativeActionProvider` — direct typed integrations (SMTP/SendGrid, HTTP, DB), the default.
- `N8nActionProvider` — delegates to n8n webhooks for teams that want the visual builder.

The side-effect guard sits **above** the provider, so exactly-once semantics hold
regardless of which provider runs.

## Consequences

- The engine runs and is fully testable with zero n8n.
- n8n becomes an opt-in convenience, added in Phase 9.
- Two code paths to keep behaviorally equivalent — covered by a shared provider contract
  test suite.

## Alternatives considered

- **n8n required (blueprint)** — operational and correctness cost too high for the core.
- **No n8n at all** — loses a genuinely useful integration surface for non-engineers.
