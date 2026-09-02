"""PlannerAgent — decompose a goal into an ordered plan.

Graph: analyze -> draft -> critique -> finalize.
"""

from __future__ import annotations

from typing import Any

from agentforge.agents.base_agent import AgentState, BaseAgent, ctx_of
from agentforge.agents.llm_helpers import ask_json, ask_text
from agentforge.core.domain.enums import CostTier
from agentforge.core.runners import StepContext, StepResult

_SYSTEM = (
    "You are a planning agent. You break a goal into a concrete, ordered plan of "
    "steps a downstream executor can follow. Be specific and avoid vague steps."
)


class _State(AgentState, total=False):
    goal: str
    constraints: list[Any]
    analysis: str
    draft_plan: Any
    critique: str
    plan: Any


class PlannerAgent(BaseAgent):
    agent_type = "planner_agent"

    def initial_state(self, ctx: StepContext) -> dict[str, Any]:
        return {
            "goal": ctx.inputs.get("goal") or ctx.inputs.get("instruction") or "",
            "constraints": ctx.inputs.get("constraints", []),
        }

    def build(self, state_graph_cls: Any, end: Any) -> Any:
        g = state_graph_cls(_State)
        g.add_node("analyze", self._analyze)
        g.add_node("draft", self._draft)
        g.add_node("critique", self._critique)
        g.add_node("finalize", self._finalize)
        g.set_entry_point("analyze")
        g.add_edge("analyze", "draft")
        g.add_edge("draft", "critique")
        g.add_edge("critique", "finalize")
        g.add_edge("finalize", end)
        return g.compile()

    async def _analyze(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        analysis = await ask_text(
            ctx,
            system=_SYSTEM,
            user=(
                f"Goal: {state['goal']}\nConstraints: {state['constraints']}\n\n"
                "List the key sub-problems and any unknowns. 3-6 bullet points."
            ),
            tier=CostTier.CHEAP,
            task_type="planning_analyze",
        )
        return {"analysis": analysis}

    async def _draft(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        plan = await ask_json(
            ctx,
            system=_SYSTEM,
            user=(
                f"Goal: {state['goal']}\nAnalysis:\n{state['analysis']}\n\n"
                "Produce a JSON array of steps: "
                '[{"step": str, "rationale": str, "depends_on": [int]}] '
                "(depends_on is 0-based indices into this array)."
            ),
            tier=CostTier.STANDARD,
            task_type="planning_draft",
        )
        return {"draft_plan": plan}

    async def _critique(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        critique = await ask_text(
            ctx,
            system=_SYSTEM,
            user=(
                f"Goal: {state['goal']}\nDraft plan: {state['draft_plan']}\n\n"
                "What is missing, risky, or out of order? Be concise."
            ),
            tier=CostTier.CHEAP,
            task_type="planning_critique",
        )
        return {"critique": critique}

    async def _finalize(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        final = await ask_json(
            ctx,
            system=_SYSTEM,
            user=(
                f"Goal: {state['goal']}\nDraft: {state['draft_plan']}\n"
                f"Critique: {state['critique']}\n\n"
                "Return the improved plan in the same JSON array shape."
            ),
            tier=CostTier.STANDARD,
            task_type="planning_finalize",
        )
        return {"plan": final}

    def to_result(self, final_state: dict[str, Any]) -> StepResult:
        return StepResult(
            output={
                "plan": final_state.get("plan", final_state.get("draft_plan", [])),
                "analysis": final_state.get("analysis", ""),
                "critique": final_state.get("critique", ""),
            }
        )
