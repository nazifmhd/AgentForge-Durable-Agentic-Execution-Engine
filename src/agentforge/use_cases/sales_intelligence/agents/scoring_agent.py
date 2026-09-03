"""ScoringAgent — score a researched lead against the ICP.

Graph: score (LLM, cheap) -> calibrate (deterministic tier + disqualifier check).

The LLM proposes a fit score and evidence; the tier is derived in code from the
ICP thresholds so the qualify/disqualify decision is auditable and not left to
the model.
"""

from __future__ import annotations

from typing import Any

from agentforge.agents.base_agent import AgentState, BaseAgent, ctx_of
from agentforge.agents.llm_helpers import ask_json
from agentforge.core.domain.enums import CostTier
from agentforge.core.runners import StepContext, StepResult
from agentforge.use_cases.sales_intelligence.models import ICPProfile, LeadScore
from agentforge.use_cases.sales_intelligence.prompts import SCORING_SYSTEM

AGENT_TYPE = "sales_scoring_agent"


class _State(AgentState, total=False):
    dossier: dict[str, Any]
    lead: dict[str, Any]
    proposal: dict[str, Any]
    score: dict[str, Any]


class ScoringAgent(BaseAgent):
    agent_type = AGENT_TYPE

    def __init__(self, icp: ICPProfile | None = None) -> None:
        super().__init__()
        self._icp = icp or ICPProfile(name="permissive-default")

    def initial_state(self, ctx: StepContext) -> dict[str, Any]:
        research = ctx.inputs.get("research", {})
        return {
            "dossier": research.get("dossier", ctx.inputs.get("dossier", {})),
            "lead": research.get("lead", ctx.inputs.get("lead", {})),
        }

    def build(self, state_graph_cls: Any, end: Any) -> Any:
        g = state_graph_cls(_State)
        g.add_node("score", self._score)
        g.add_node("calibrate", self._calibrate)
        g.set_entry_point("score")
        g.add_edge("score", "calibrate")
        g.add_edge("calibrate", end)
        return g.compile()

    async def _score(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        icp = self._icp
        proposal = await ask_json(
            ctx,
            system=SCORING_SYSTEM,
            user=(
                f"ICP: industries={icp.target_industries}, headcount "
                f"{icp.min_headcount}-{icp.max_headcount}, titles={icp.target_titles}, "
                f"required signals={icp.required_signals}, disqualifiers={icp.disqualifiers}\n\n"
                f"Lead: {state.get('lead')}\nDossier: {state.get('dossier')}\n\n"
                'Return {"fit_score": 0-100 int, "rationale": str, "matched_criteria": '
                '[str], "missing_criteria": [str], "disqualifier_hits": [str], '
                '"recommended_action": str}.'
            ),
            tier=CostTier.CHEAP,
            task_type="sales_scoring",
        )
        return {"proposal": proposal if isinstance(proposal, dict) else {}}

    async def _calibrate(self, state: dict[str, Any]) -> dict[str, Any]:
        p = state.get("proposal") or {}
        icp = self._icp
        raw_score = p.get("fit_score", 0)
        fit = max(0, min(100, int(raw_score) if isinstance(raw_score, int | float) else 0))
        hits = [str(h) for h in p.get("disqualifier_hits", []) if h]

        if hits or fit < icp.qualify_threshold:
            tier = "disqualified"
        elif fit >= icp.hot_threshold:
            tier = "hot"
        elif fit >= icp.warm_threshold:
            tier = "warm"
        else:
            tier = "cold"

        score = LeadScore(
            fit_score=fit,
            tier=tier,
            rationale=str(p.get("rationale", "")),
            matched_criteria=[str(x) for x in p.get("matched_criteria", [])],
            missing_criteria=[str(x) for x in p.get("missing_criteria", [])],
            disqualifier_hits=hits,
            recommended_action=str(p.get("recommended_action", "")),
        )
        return {"score": score.model_dump()}

    def to_result(self, final_state: dict[str, Any]) -> StepResult:
        # Output IS the score dict, so a downstream step's inputs["score"] is the
        # LeadScore shape directly (no extra nesting).
        return StepResult(output=final_state.get("score", {}))
