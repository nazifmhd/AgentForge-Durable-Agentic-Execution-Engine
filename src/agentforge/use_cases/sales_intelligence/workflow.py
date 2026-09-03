"""The Sales Intelligence & Outreach workflow definition.

    research ──▶ score ──▶ draft_outreach ──▶ send

- ``research`` builds a dossier (LLM + CRM/enrichment tools).
- ``score`` qualifies the lead against the ICP; a "disqualified" tier makes the
  two downstream steps no-ops (no LLM spend, no side effects).
- ``draft_outreach`` writes + self-reviews the email / LinkedIn copy.
- ``send`` creates a CRM task and enqueues the email as exactly-once effects.
  It ``requires_approval`` by default; if the deadline passes with no decision the
  auto-action is ``skip``. ``on_failure=ROLLBACK`` unwinds the CRM task if the
  send fails.
"""

from __future__ import annotations

from agentforge.core.domain.definition import RetryPolicy, WorkflowDefinition, WorkflowStep
from agentforge.core.domain.enums import CostTier, OnFailure
from agentforge.use_cases.sales_intelligence.agents.copywriting_agent import (
    AGENT_TYPE as COPYWRITING_AGENT,
)
from agentforge.use_cases.sales_intelligence.agents.outreach_agent import (
    AGENT_TYPE as OUTREACH_AGENT,
)
from agentforge.use_cases.sales_intelligence.agents.outreach_agent import COMPENSATOR_TYPE
from agentforge.use_cases.sales_intelligence.agents.research_agent import (
    AGENT_TYPE as RESEARCH_AGENT,
)
from agentforge.use_cases.sales_intelligence.agents.scoring_agent import AGENT_TYPE as SCORING_AGENT
from agentforge.use_cases.sales_intelligence.provider import CREATE_CRM_TASK, SEND_EMAIL

WORKFLOW_ID = "sales-intelligence"

_LLM_RETRY = RetryPolicy(max_retries=2)


def sales_intelligence_workflow(
    *,
    version: str = "1.0.0",
    require_send_approval: bool = True,
    approval_timeout_seconds: int | None = 86_400,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        name="Sales Intelligence & Outreach",
        description="Research a lead, qualify it against the ICP, and send first-touch outreach.",
        version=version,
        on_failure=OnFailure.ROLLBACK,
        max_concurrent_steps=2,
        steps=(
            WorkflowStep(
                step_id="research",
                name="Research the lead",
                agent_type=RESEARCH_AGENT,
                cost_tier=CostTier.STANDARD,
                timeout_seconds=180,
                retry_policy=_LLM_RETRY,
            ),
            WorkflowStep(
                step_id="score",
                name="Score against ICP",
                agent_type=SCORING_AGENT,
                cost_tier=CostTier.CHEAP,
                timeout_seconds=120,
                retry_policy=_LLM_RETRY,
                dependencies=("research",),
            ),
            WorkflowStep(
                step_id="draft_outreach",
                name="Draft outreach",
                agent_type=COPYWRITING_AGENT,
                cost_tier=CostTier.STANDARD,
                timeout_seconds=180,
                retry_policy=_LLM_RETRY,
                dependencies=("research", "score"),
            ),
            WorkflowStep(
                step_id="send",
                name="Send outreach",
                agent_type=OUTREACH_AGENT,
                cost_tier=CostTier.CHEAP,
                timeout_seconds=120,
                dependencies=("research", "score", "draft_outreach"),
                side_effects=(CREATE_CRM_TASK, SEND_EMAIL),
                compensation_action=COMPENSATOR_TYPE,
                requires_approval=require_send_approval,
                approval_timeout_seconds=approval_timeout_seconds,
                approval_auto_action="skip",
            ),
        ),
    )
