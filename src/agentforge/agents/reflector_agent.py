"""ReflectorAgent — self-correct an output given validation issues.

Graph: diagnose -> revise.
"""

from __future__ import annotations

from typing import Any

from agentforge.agents.base_agent import AgentState, BaseAgent, ctx_of
from agentforge.agents.llm_helpers import ask_json, ask_text
from agentforge.core.domain.enums import CostTier
from agentforge.core.runners import StepContext, StepResult

_SYSTEM = (
    "You are a self-correction agent. Given an output and the problems found in "
    "it, you produce a corrected version that resolves every problem."
)


class _State(AgentState, total=False):
    output: Any
    issues: list[Any]
    task: str
    diagnosis: str
    revised: dict[str, Any]


class ReflectorAgent(BaseAgent):
    agent_type = "reflector_agent"

    def initial_state(self, ctx: StepContext) -> dict[str, Any]:
        return {
            "output": ctx.inputs.get("output", ctx.inputs),
            "issues": ctx.inputs.get("issues", []),
            "task": ctx.inputs.get("task", ctx.inputs.get("instruction", "")),
        }

    def build(self, state_graph_cls: Any, end: Any) -> Any:
        g = state_graph_cls(_State)
        g.add_node("diagnose", self._diagnose)
        g.add_node("revise", self._revise)
        g.set_entry_point("diagnose")
        g.add_edge("diagnose", "revise")
        g.add_edge("revise", end)
        return g.compile()

    async def _diagnose(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        diagnosis = await ask_text(
            ctx,
            system=_SYSTEM,
            user=(
                f"Task: {state['task']}\nOutput: {state['output']}\n"
                f"Problems found: {state['issues']}\n\n"
                "For each problem, state the root cause and the fix. Be concise."
            ),
            tier=CostTier.CHEAP,
            task_type="reflect_diagnose",
        )
        return {"diagnosis": diagnosis}

    async def _revise(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        revised = await ask_json(
            ctx,
            system=_SYSTEM,
            user=(
                f"Task: {state['task']}\nOriginal output: {state['output']}\n"
                f"Diagnosis: {state['diagnosis']}\n\n"
                'Return {"corrected_output": <the fixed output>, "changes": [str]}.'
            ),
            tier=CostTier.STANDARD,
            task_type="reflect_revise",
        )
        return {"revised": revised}

    def to_result(self, final_state: dict[str, Any]) -> StepResult:
        revised = final_state.get("revised") or {}
        return StepResult(
            output={
                "corrected_output": revised.get("corrected_output", final_state.get("output")),
                "changes": revised.get("changes", []),
                "diagnosis": final_state.get("diagnosis", ""),
            }
        )
