"""``LLMProvider`` — a thin, provider-neutral surface over a single completion.

Agents never construct a provider client; they call ``StepContext.llm`` which
routes (cost-aware) to a model and dispatches here. Providers normalise their
SDK's errors onto the engine's ``LLMError`` hierarchy so the retry classifier
works uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agentforge.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    model_id: str
    messages: list[LLMMessage]
    system: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 4096
    stop: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [{"role": m.role, "content": m.content} for m in self.messages]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMResponse:
    model_id: str
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_input: int = 0
    tokens_output: int = 0
    stop_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def complete(self, req: LLMRequest) -> LLMResponse: ...

    async def count_tokens(self, req: LLMRequest) -> int | None:
        """Exact input token count, or ``None`` if the provider can't/ won't."""
        ...


class LLMProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}

    def register(self, provider: LLMProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> LLMProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise ConfigurationError(f"no LLM provider registered as {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._providers
