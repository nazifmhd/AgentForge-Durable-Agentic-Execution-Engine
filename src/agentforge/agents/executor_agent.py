"""ExecutorAgent — carry out one instruction, optionally calling tools.

Graph: decide -> (act -> decide)* -> respond. A hard iteration cap stops runaway
tool loops.
"""

from __future__ import annotations

from typing import Any

from agentforge.agents.base_agent import AgentState, BaseAgent, ctx_of
from agentforge.agents.llm_helpers import ask_json, ask_text
from agentforge.agents.tools import ToolRegistry
from agentforge.core.domain.enums import CostTier
from agentforge.core.runners import StepContext, StepResult

_MAX_TOOL_CALLS = 5
_SYSTEM = (
    "You are an execution agent. You carry out one instruction precisely. "
    "Use a tool only when it is genuinely needed; otherwise answer directly."
)


class _State(AgentState, total=False):
    instruction: str
    tool_results: list[dict[str, Any]]
    iterations: int
    decision: dict[str, Any]
    result: str


class ExecutorAgent(BaseAgent):
    agent_type = "executor_agent"

    def __init__(self, tools: ToolRegistry | None = None) -> None:
        super().__init__()
        self._tools = tools or ToolRegistry()

    def initial_state(self, ctx: StepContext) -> dict[str, Any]:
        return {
            "instruction": ctx.inputs.get("instruction")
            or ctx.inputs.get("goal")
            or str(ctx.inputs),
            "tool_results": [],
            "iterations": 0,
        }

    def build(self, state_graph_cls: Any, end: Any) -> Any:
        g = state_graph_cls(_State)
        g.add_node("decide", self._decide)
        g.add_node("act", self._act)
        g.add_node("respond", self._respond)
        g.set_entry_point("decide")
        g.add_conditional_edges("decide", self._route, {"act": "act", "respond": "respond"})
        g.add_edge("act", "decide")
        g.add_edge("respond", end)
        return g.compile()

    async def _decide(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        decision = await ask_json(
            ctx,
            system=_SYSTEM,
            user=(
                f"Instruction: {state['instruction']}\n"
                f"Tools:\n{self._tools.describe()}\n"
                f"Tool results so far: {state['tool_results']}\n\n"
                'Return {"action": "tool"|"final", "tool": str|null, '
                '"args": object|null, "answer": str|null}.'
            ),
            tier=CostTier.STANDARD,
            task_type="execute_decide",
        )
        return {"decision": decision, "iterations": state["iterations"] + 1}

    def _route(self, state: dict[str, Any]) -> str:
        decision = state.get("decision") or {}
        tool = decision.get("tool")
        wants_tool = (
            decision.get("action") == "tool"
            and isinstance(tool, str)
            and tool in self._tools
            and state["iterations"] < _MAX_TOOL_CALLS
        )
        return "act" if wants_tool else "respond"

    async def _act(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        decision = state["decision"]
        tool = self._tools.get(decision["tool"])
        result = await tool.call(ctx, decision.get("args") or {})
        return {
            "tool_results": [
                *state["tool_results"],
                {"tool": tool.name, "args": decision.get("args"), "result": result},
            ]
        }

    async def _respond(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        decision = state.get("decision") or {}
        if decision.get("action") == "final" and decision.get("answer"):
            return {"result": decision["answer"]}
        answer = await ask_text(
            ctx,
            system=_SYSTEM,
            user=(
                f"Instruction: {state['instruction']}\n"
                f"Tool results: {state['tool_results']}\n\n"
                "Give the final answer."
            ),
            tier=CostTier.STANDARD,
            task_type="execute_respond",
        )
        return {"result": answer}

    def to_result(self, final_state: dict[str, Any]) -> StepResult:
        return StepResult(
            output={
                "result": final_state.get("result", ""),
                "tool_calls": final_state.get("tool_results", []),
            }
        )
