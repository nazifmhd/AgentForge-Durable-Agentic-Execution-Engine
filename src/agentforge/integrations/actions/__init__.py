"""Pluggable external-action providers (ADR-0007)."""

from agentforge.integrations.actions.base import (
    ActionProvider,
    BaseActionProvider,
    EffectRequest,
    EffectResult,
    ProviderRegistry,
)
from agentforge.integrations.actions.n8n import N8nActionProvider, build_n8n_provider
from agentforge.integrations.actions.providers import (
    HttpActionProvider,
    NoopActionProvider,
)

__all__ = [
    "ActionProvider",
    "BaseActionProvider",
    "EffectRequest",
    "EffectResult",
    "HttpActionProvider",
    "N8nActionProvider",
    "NoopActionProvider",
    "ProviderRegistry",
    "build_n8n_provider",
]
