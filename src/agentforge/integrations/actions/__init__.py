"""Pluggable external-action providers (ADR-0007)."""

from agentforge.integrations.actions.base import (
    ActionProvider,
    BaseActionProvider,
    EffectRequest,
    EffectResult,
    ProviderRegistry,
)
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
    "NoopActionProvider",
    "ProviderRegistry",
]
