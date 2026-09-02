"""Tools an agent can call during a step.

Read-only / pure tools call straight through. A tool with an external side effect
should route through ``StepContext.execute_effect`` so it stays exactly-once
(ADR-0003); :class:`EffectTool` does that wiring.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from agentforge.core.runners import StepContext
from agentforge.exceptions import ConfigurationError


@runtime_checkable
class AgentTool(Protocol):
    name: str
    description: str

    async def call(self, ctx: StepContext, args: dict[str, Any]) -> Any: ...


class FunctionTool:
    def __init__(
        self,
        name: str,
        description: str,
        fn: Callable[[StepContext, dict[str, Any]], Awaitable[Any]],
    ) -> None:
        self.name = name
        self.description = description
        self._fn = fn

    async def call(self, ctx: StepContext, args: dict[str, Any]) -> Any:
        return await self._fn(ctx, args)


class EffectTool:
    """A tool whose action is an external side effect — routed through the guard."""

    def __init__(
        self, name: str, description: str, *, effect_name: str, provider: str = "noop"
    ) -> None:
        self.name = name
        self.description = description
        self._effect_name = effect_name
        self._provider = provider

    async def call(self, ctx: StepContext, args: dict[str, Any]) -> Any:
        result = await ctx.execute_effect(self._effect_name, args, provider=self._provider)
        return result.data


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool:
        try:
            return self._tools[name]
        except KeyError:
            raise ConfigurationError(f"no tool named {name!r}") from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> str:
        if not self._tools:
            return "(no tools available)"
        return "\n".join(f"- {t.name}: {t.description}" for t in self._tools.values())

    def __contains__(self, name: str) -> bool:
        return name in self._tools
