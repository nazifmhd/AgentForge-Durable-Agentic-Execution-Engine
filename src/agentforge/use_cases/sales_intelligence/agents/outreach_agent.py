"""OutreachAgent — dispatch the approved outreach as exactly-once side effects.

Graph: gate -> dispatch.

``gate`` short-circuits when the lead was disqualified or copywriting was
skipped. ``dispatch`` creates a CRM task and enqueues the email through
``StepContext.execute_effect`` (outbox + provider idempotency key), so a crash
between the two — or a retry of the whole step — never double-sends.

The workflow marks this step ``compensation_action="sales_outreach_compensator"``
so a later rollback cancels the CRM task and recalls the email.
"""

from __future__ import annotations

from typing import Any

from agentforge.agents.base_agent import AgentState, BaseAgent, ctx_of
from agentforge.core.runners import StepContext, StepResult
from agentforge.use_cases.sales_intelligence.models import DispatchReceipt
from agentforge.use_cases.sales_intelligence.provider import CREATE_CRM_TASK, SEND_EMAIL

AGENT_TYPE = "sales_outreach_agent"
COMPENSATOR_TYPE = "sales_outreach_compensator"

_PROVIDER = "sales"


class _State(AgentState, total=False):
    lead: dict[str, Any]
    score: dict[str, Any]
    outreach: dict[str, Any]
    skipped: bool
    reason: str
    receipt: dict[str, Any]


class OutreachAgent(BaseAgent):
    agent_type = AGENT_TYPE

    def __init__(self, *, provider_name: str = _PROVIDER) -> None:
        super().__init__()
        self._provider = provider_name

    def initial_state(self, ctx: StepContext) -> dict[str, Any]:
        research = ctx.inputs.get("research", {})
        copy = ctx.inputs.get("draft_outreach", {})
        skipped = bool(copy.get("skipped"))
        return {
            "lead": research.get("lead", ctx.inputs.get("lead", {})),
            "score": ctx.inputs.get("score", {}),
            "outreach": copy.get("outreach", {}),
            "skipped": skipped,
            "reason": copy.get("reason", "lead disqualified") if skipped else "",
        }

    def build(self, state_graph_cls: Any, end: Any) -> Any:
        g = state_graph_cls(_State)
        g.add_node("dispatch", self._dispatch)
        g.set_entry_point("dispatch")
        g.add_edge("dispatch", end)
        return g.compile()

    async def _dispatch(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("skipped") or not state.get("outreach"):
            receipt = DispatchReceipt(skipped=True, reason=state.get("reason", ""))
            return {"receipt": receipt.model_dump()}

        ctx = ctx_of(state)
        lead = state.get("lead") or {}
        score = state.get("score") or {}
        outreach = state["outreach"]
        email = outreach.get("email", {})
        tier = score.get("tier", "lead")

        task = await ctx.execute_effect(
            CREATE_CRM_TASK,
            {
                "company": lead.get("company_name", ""),
                "contact": lead.get("contact_name", ""),
                "contact_email": lead.get("contact_email", ""),
                "tier": tier,
                "summary": f"Outreach queued ({tier}): {score.get('rationale', '')}",
            },
            provider=self._provider,
        )
        sent = await ctx.execute_effect(
            SEND_EMAIL,
            {
                "to": lead.get("contact_email", ""),
                "subject": email.get("subject", ""),
                "body": email.get("body", ""),
            },
            provider=self._provider,
        )
        receipt = DispatchReceipt(
            skipped=False,
            crm_task_ref=str(task.data.get("ref")) if task.data.get("ref") else None,
            email_ref=str(sent.data.get("ref")) if sent.data.get("ref") else None,
        )
        return {"receipt": receipt.model_dump()}

    def to_result(self, final_state: dict[str, Any]) -> StepResult:
        receipt = final_state.get("receipt") or DispatchReceipt(skipped=True).model_dump()
        return StepResult(output=receipt)


class OutreachCompensator:
    """Registered under ``sales_outreach_compensator``. The side effects are undone
    by ``SideEffectGuard.compensate_instance`` before this runs; this handler just
    records that the step was rolled back."""

    async def run(self, ctx: StepContext) -> StepResult:
        prior = ctx.inputs.get("output", {})
        return StepResult(
            output={
                "compensated": True,
                "crm_task_ref": prior.get("crm_task_ref"),
                "email_ref": prior.get("email_ref"),
            }
        )
