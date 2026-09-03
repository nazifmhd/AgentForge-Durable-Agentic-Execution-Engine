"""CopywritingAgent — write first-touch outreach for a qualified lead.

Graph: gate -> draft -> review -> finalize.

``gate`` short-circuits to END (no LLM spend) when the lead was disqualified.
``finalize`` runs one revision pass only if the reviewer flagged issues — the
draft/critique/revise loop is contained in this single step.
"""

from __future__ import annotations

from typing import Any

from agentforge.agents.base_agent import AgentState, BaseAgent, ctx_of
from agentforge.agents.llm_helpers import ask_json
from agentforge.core.domain.enums import CostTier
from agentforge.core.runners import StepContext, StepResult
from agentforge.use_cases.sales_intelligence.models import Channel, OutreachDraft, OutreachPackage
from agentforge.use_cases.sales_intelligence.prompts import COPY_REVIEW_SYSTEM, COPYWRITING_SYSTEM

AGENT_TYPE = "sales_copywriting_agent"


class _State(AgentState, total=False):
    lead: dict[str, Any]
    dossier: dict[str, Any]
    score: dict[str, Any]
    email: dict[str, Any]
    linkedin: dict[str, Any]
    issues: list[str]
    revisions: int
    skipped: bool


class CopywritingAgent(BaseAgent):
    agent_type = AGENT_TYPE

    def initial_state(self, ctx: StepContext) -> dict[str, Any]:
        research = ctx.inputs.get("research", {})
        return {
            "lead": research.get("lead", ctx.inputs.get("lead", {})),
            "dossier": research.get("dossier", ctx.inputs.get("dossier", {})),
            "score": ctx.inputs.get("score", {}),
            "revisions": 0,
        }

    def build(self, state_graph_cls: Any, end: Any) -> Any:
        g = state_graph_cls(_State)
        g.add_node("draft", self._draft)
        g.add_node("review", self._review)
        g.add_node("finalize", self._finalize)
        g.set_entry_point("draft")
        g.add_conditional_edges("draft", self._after_draft, {"review": "review", "skip": end})
        g.add_edge("review", "finalize")
        g.add_edge("finalize", end)
        return g.compile()

    def _after_draft(self, state: dict[str, Any]) -> str:
        return "skip" if state.get("skipped") else "review"

    async def _draft(self, state: dict[str, Any]) -> dict[str, Any]:
        if (state.get("score") or {}).get("tier") == "disqualified":
            return {"skipped": True}
        ctx = ctx_of(state)
        drafts = await ask_json(
            ctx,
            system=COPYWRITING_SYSTEM,
            user=(
                f"Lead: {state['lead']}\nDossier: {state['dossier']}\n"
                f"Why now (score rationale): {(state.get('score') or {}).get('rationale', '')}\n\n"
                'Return {"email": {"subject": str, "body": str, "call_to_action": str, '
                '"personalization_notes": [str]}, "linkedin": {"body": str, '
                '"call_to_action": str, "personalization_notes": [str]}}.'
            ),
            tier=CostTier.STANDARD,
            task_type="sales_copywriting",
        )
        d = drafts if isinstance(drafts, dict) else {}
        return {
            "email": _draft_of("email", d.get("email")),
            "linkedin": _draft_of("linkedin", d.get("linkedin")),
        }

    async def _review(self, state: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx_of(state)
        verdict = await ask_json(
            ctx,
            system=COPY_REVIEW_SYSTEM,
            user=(
                f"Email: {state.get('email')}\nLinkedIn: {state.get('linkedin')}\n\n"
                'Return {"issues": [str]} — every house-style violation, empty if clean.'
            ),
            tier=CostTier.CHEAP,
            task_type="sales_copy_review",
        )
        issues = verdict.get("issues", []) if isinstance(verdict, dict) else []
        return {"issues": [str(i) for i in issues if i]}

    async def _finalize(self, state: dict[str, Any]) -> dict[str, Any]:
        issues = state.get("issues") or []
        if not issues:
            return {}
        ctx = ctx_of(state)
        fixed = await ask_json(
            ctx,
            system=COPYWRITING_SYSTEM,
            user=(
                f"Email: {state.get('email')}\nLinkedIn: {state.get('linkedin')}\n"
                f"Fix these issues: {issues}\n\n"
                "Return the corrected drafts in the same JSON shape as before."
            ),
            tier=CostTier.STANDARD,
            task_type="sales_copy_revise",
        )
        d = fixed if isinstance(fixed, dict) else {}
        out: dict[str, Any] = {"revisions": state.get("revisions", 0) + 1}
        if d.get("email"):
            out["email"] = _draft_of("email", d["email"])
        if d.get("linkedin"):
            out["linkedin"] = _draft_of("linkedin", d["linkedin"])
        return out

    def to_result(self, final_state: dict[str, Any]) -> StepResult:
        if final_state.get("skipped"):
            return StepResult(output={"skipped": True, "reason": "lead disqualified"})
        package = OutreachPackage(
            email=OutreachDraft.model_validate(final_state["email"]),
            linkedin=OutreachDraft.model_validate(final_state["linkedin"]),
            revisions=final_state.get("revisions", 0),
            quality_issues=final_state.get("issues", []),
            send_ready=True,
        )
        return StepResult(output={"skipped": False, "outreach": package.model_dump()})


def _draft_of(channel: Channel, raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    return OutreachDraft(
        channel=channel,
        subject=str(data.get("subject", "")),
        body=str(data.get("body", "")),
        call_to_action=str(data.get("call_to_action", "")),
        personalization_notes=[str(x) for x in data.get("personalization_notes", [])],
    ).model_dump()
