"""``BaseAgent`` — a LangGraph ``StateGraph`` wrapped as a :class:`StepRunner`.

AgentForge owns durability at the step boundary (ADR-0006), so the LangGraph
checkpointer is never used: an agent's internal graph runs to completion within
one step attempt. The per-run :class:`StepContext` is threaded through the graph
state (not stored on the instance) so the same agent object can drive many steps
concurrently.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypedDict

from agentforge.core.runners import StepContext, StepResult
from agentforge.exceptions import ConfigurationError
from agentforge.logging import get_logger

log = get_logger("agent")

_RECURSION_LIMIT = 40


class AgentState(TypedDict, total=False):
    """Base graph state. Subclass it (``total=False``) to add per-agent fields.

    ``ctx`` carries the per-run :class:`StepContext` through the graph as its own
    channel, so partial node updates merge instead of replacing the whole state
    and concurrent step attempts never share it.
    """

    ctx: Any


def _load_langgraph() -> Any:
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:  # pragma: no cover - depends on the `agents` extra
        raise ConfigurationError(
            "langgraph is required for the agent runtime (install the 'agents' extra)"
        ) from exc
    return StateGraph, END


class BaseAgent(ABC):
    """Subclasses implement ``build`` (returns a compiled graph), ``initial_state``
    and ``to_result``. ``run`` is the :class:`StepRunner` entry point."""

    agent_type: str = "base"

    def __init__(self) -> None:
        self._graph: Any | None = None

    def _compiled(self) -> Any:
        if self._graph is None:
            StateGraph, END = _load_langgraph()
            self._graph = self.build(StateGraph, END)
        return self._graph

    @abstractmethod
    def build(self, state_graph_cls: Any, end: Any) -> Any:
        """Return a compiled LangGraph graph."""

    @abstractmethod
    def initial_state(self, ctx: StepContext) -> dict[str, Any]: ...

    @abstractmethod
    def to_result(self, final_state: dict[str, Any]) -> StepResult: ...

    async def run(self, ctx: StepContext) -> StepResult:
        graph = self._compiled()
        state = self.initial_state(ctx)
        state["ctx"] = ctx
        final: dict[str, Any] = await graph.ainvoke(
            state, config={"recursion_limit": _RECURSION_LIMIT}
        )
        return self.to_result(final)


def ctx_of(state: dict[str, Any]) -> StepContext:
    ctx = state.get("ctx")
    if not isinstance(ctx, StepContext):  # pragma: no cover - programmer error
        raise ConfigurationError("agent graph state is missing its StepContext")
    return ctx
