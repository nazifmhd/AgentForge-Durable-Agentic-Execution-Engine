"""ResearchAgent — assemble a dossier on a lead.

Graph: gather (CRM + web tools) -> synthesize (LLM) -> score_confidence.
"""

from __future__ import annotations

from typing import Any

from agentforge.agents.base_agent import AgentState, BaseAgent, ctx_of
from agentforge.agents.llm_helpers import ask_json
from agentforge.core.domain.enums import CostTier
from agentforge.core.runners import StepContext, StepResult
from agentforge.use_cases.sales_intelligence.models import Lead, ResearchDossier
from agentforge.use_cases.sales_intelligence.prompts import RESEARCH_SYSTEM
from agentforge.use_cases.sales_intelligence.tools import CRM_LOOKUP, WEB_ENRICH, build_sales_tools

AGENT_TYPE = "sales_research_agent"


class _State(AgentState, total=False):
    lead: dict[str, Any]
    crm: dict[str, Any]
    enrichment: dict[str, Any]
    dossier: dict[str, Any]


class ResearchAgent(BaseAgent):
    agent_type = AGENT_TYPE

    def __init__(self, tools: Any | None = None) -> None:
        super().__init__()
        self._tools = tools or build_sales_tools()

    def initial_state(self, ctx: StepContext) -> dict[str, Any]:
        lead = Lead.from_context(ctx.inputs)
        return {"lead": lead.model_dump()}

    def build(self, state_graph_cls: Any, end: Any) -> Any:
        g = state_graph_cls(_State)
        g.add_node("gather", self._gather)
        g.add_node("synthesize", self._synthesize)
        g.set_entry_point("gather")
        g.add_edge("gather", "synthesize")
        g.add_edge("synthesize", end)
        return g.compile()

    async def _gather(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        lead = state["lead"]
        args = {"domain": lead.get("domain", ""), "company_name": lead.get("company_name", "")}
        crm = await self._tools.get(CRM_LOOKUP).call(ctx, args)
        enrichment = await self._tools.get(WEB_ENRICH).call(ctx, {"domain": lead.get("domain", "")})
        return {"crm": dict(crm), "enrichment": dict(enrichment)}

    async def _synthesize(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        raw = await ask_json(
            ctx,
            system=RESEARCH_SYSTEM,
            user=(
                f"Lead: {state['lead']}\n"
                f"CRM record: {state.get('crm')}\n"
                f"Enrichment data: {state.get('enrichment')}\n\n"
                "Build the dossier. Return JSON with keys: company_summary (str), "
                "industry (str), headcount_estimate (int or null), tech_stack (list[str]), "
                "buying_signals (list[str]), recent_news (list[str]), pain_hypotheses "
                "(list[str]), sources (list[str]), confidence (0.0-1.0 float)."
            ),
            tier=CostTier.STANDARD,
            task_type="sales_research",
        )
        dossier = _coerce_dossier(raw, already_in_crm=bool((state.get("crm") or {}).get("found")))
        return {"dossier": dossier.model_dump()}

    def to_result(self, final_state: dict[str, Any]) -> StepResult:
        return StepResult(
            output={
                "lead": final_state["lead"],
                "dossier": final_state.get("dossier", ResearchDossier().model_dump()),
            }
        )


def _coerce_dossier(raw: Any, *, already_in_crm: bool) -> ResearchDossier:
    data = raw if isinstance(raw, dict) else {}
    known = {k: data[k] for k in ResearchDossier.model_fields if k in data}
    known["already_in_crm"] = already_in_crm
    try:
        return ResearchDossier.model_validate(known)
    except ValueError:
        return ResearchDossier(already_in_crm=already_in_crm, confidence=0.0)
