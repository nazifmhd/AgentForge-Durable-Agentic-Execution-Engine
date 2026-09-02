"""Provider-neutral LLM surface + concrete providers.

The concrete providers import their SDKs lazily (they live in the ``agents``
extra), so importing this package never requires ``anthropic`` / ``openai``.
"""

from agentforge.integrations.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMProviderRegistry,
    LLMRequest,
    LLMResponse,
    ToolCall,
)

__all__ = [
    "LLMMessage",
    "LLMProvider",
    "LLMProviderRegistry",
    "LLMRequest",
    "LLMResponse",
    "ToolCall",
    "build_provider",
]


def build_provider(name: str, **kwargs: object) -> LLMProvider:
    """Lazily construct a provider by name (``anthropic`` | ``openai``)."""
    if name == "anthropic":
        from agentforge.integrations.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(**kwargs)  # type: ignore[arg-type]
    if name == "openai":
        from agentforge.integrations.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(**kwargs)  # type: ignore[arg-type]
    from agentforge.exceptions import ConfigurationError

    raise ConfigurationError(f"unknown LLM provider {name!r}")
