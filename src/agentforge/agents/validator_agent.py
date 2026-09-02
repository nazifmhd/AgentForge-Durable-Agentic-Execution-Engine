"""ValidatorAgent — check an output against criteria / an expected shape.

Graph: check_structure -> check_content -> verdict.
"""

from __future__ import annotations

from typing import Any

from agentforge.agents.base_agent import AgentState, BaseAgent, ctx_of
from agentforge.agents.llm_helpers import ask_json
from agentforge.core.domain.enums import CostTier
from agentforge.core.runners import StepContext, StepResult

_SYSTEM = (
    "You are a validation agent. You judge whether an output meets its "
    "requirements. Be strict and specific about what is wrong."
)


class _State(AgentState, total=False):
    output: Any
    criteria: str
    schema_hint: str
    structure: dict[str, Any]
    content: dict[str, Any]
    valid: bool
    issues: list[str]
    score: float


class ValidatorAgent(BaseAgent):
    agent_type = "validator_agent"

    def initial_state(self, ctx: StepContext) -> dict[str, Any]:
        return {
            "output": ctx.inputs.get("output", ctx.inputs),
            "criteria": ctx.inputs.get("criteria", ""),
            "schema_hint": ctx.inputs.get("schema_hint", ""),
        }

    def build(self, state_graph_cls: Any, end: Any) -> Any:
        g = state_graph_cls(_State)
        g.add_node("check_structure", self._check_structure)
        g.add_node("check_content", self._check_content)
        g.add_node("verdict", self._verdict)
        g.set_entry_point("check_structure")
        g.add_edge("check_structure", "check_content")
        g.add_edge("check_content", "verdict")
        g.add_edge("verdict", end)
        return g.compile()

    async def _check_structure(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        result = await ask_json(
            ctx,
            system=_SYSTEM,
            user=(
                f"Output: {state['output']}\nExpected shape: {state['schema_hint'] or 'any'}\n\n"
                'Return {"structure_ok": bool, "structure_issues": [str]}.'
            ),
            tier=CostTier.CHEAP,
            task_type="validate_structure",
        )
        return {"structure": result}

    async def _check_content(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        result = await ask_json(
            ctx,
            system=_SYSTEM,
            user=(
                f"Output: {state['output']}\n"
                f"Criteria: {state['criteria'] or 'be correct and complete'}\n\n"
                'Return {"content_ok": bool, "content_issues": [str]}.'
            ),
            tier=CostTier.STANDARD,
            task_type="validate_content",
        )
        return {"content": result}

    async def _verdict(self, state: dict[str, Any]) -> dict[str, Any]:
        structure = state.get("structure") or {}
        content = state.get("content") or {}
        issues = [
            *structure.get("structure_issues", []),
            *content.get("content_issues", []),
        ]
        valid = bool(structure.get("structure_ok", True)) and bool(content.get("content_ok", True))
        score = 1.0 if valid else max(0.0, 1.0 - 0.25 * len(issues))
        return {"valid": valid, "issues": issues, "score": score}

    def to_result(self, final_state: dict[str, Any]) -> StepResult:
        return StepResult(
            output={
                "valid": final_state.get("valid", False),
                "issues": final_state.get("issues", []),
                "score": final_state.get("score", 0.0),
            }
        )
