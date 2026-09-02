"""``ActionProvider`` — the seam between the engine and the outside world.

``core`` never performs an external action directly; it hands an
:class:`EffectRequest` to a provider through the :class:`SideEffectGuard`
(ADR-0007). A provider that supports idempotency keys gives exactly-once; one
that doesn't gives at-least-once-with-dedup plus an optional ``reconcile`` to
close the crash window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agentforge.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class EffectRequest:
    effect_name: str
    params: dict[str, Any]
    idempotency_key: str
    instance_id: str
    tenant_id: str
    step_id: str


@dataclass(frozen=True, slots=True)
class EffectResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    provider_ref: str | None = None  # external id (message id, row id, …)
    error: str | None = None


@runtime_checkable
class ActionProvider(Protocol):
    name: str

    def supports_idempotency_key(self, effect_name: str) -> bool: ...

    async def execute(self, req: EffectRequest) -> EffectResult: ...

    async def reconcile(self, req: EffectRequest) -> EffectResult | None:
        """Best-effort 'did a previous attempt already land?'. ``None`` = unknown."""
        ...

    async def compensate(self, req: EffectRequest, executed: EffectResult) -> EffectResult | None:
        """Undo a previously executed effect. ``None`` = nothing to undo."""
        ...


class BaseActionProvider:
    """Sensible defaults: no idempotency support, no reconcile, no compensation."""

    name: str = "base"

    def supports_idempotency_key(self, effect_name: str) -> bool:
        return False

    async def execute(self, req: EffectRequest) -> EffectResult:  # pragma: no cover
        raise NotImplementedError

    async def reconcile(self, req: EffectRequest) -> EffectResult | None:
        return None

    async def compensate(self, req: EffectRequest, executed: EffectResult) -> EffectResult | None:
        return None


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ActionProvider] = {}

    def register(self, provider: ActionProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> ActionProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise ConfigurationError(f"no action provider registered as {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._providers
