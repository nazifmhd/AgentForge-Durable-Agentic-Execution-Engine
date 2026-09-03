"""Sales Intelligence & Outreach — the reference workflow built on AgentForge.

It exercises the whole engine: LangGraph agents, cost-tiered LLM calls with
pre-flight budgeting, read-only tools, exactly-once side effects with
compensation, and human-in-the-loop approval on the send step.

    from agentforge.use_cases.sales_intelligence import (
        register_sales_intelligence, sales_intelligence_workflow,
    )
    register_sales_intelligence(engine.registry, icp=load_icp())
    await engine.definitions.register(sales_intelligence_workflow(), tenant_id=...)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentforge.use_cases.sales_intelligence.agents.copywriting_agent import CopywritingAgent
from agentforge.use_cases.sales_intelligence.agents.outreach_agent import (
    COMPENSATOR_TYPE,
    OutreachAgent,
    OutreachCompensator,
)
from agentforge.use_cases.sales_intelligence.agents.research_agent import ResearchAgent
from agentforge.use_cases.sales_intelligence.agents.scoring_agent import ScoringAgent
from agentforge.use_cases.sales_intelligence.config import DEFAULT_ICP_PATH, default_icp, load_icp
from agentforge.use_cases.sales_intelligence.models import (
    DispatchReceipt,
    ICPProfile,
    Lead,
    LeadScore,
    OutreachDraft,
    OutreachPackage,
    ResearchDossier,
)
from agentforge.use_cases.sales_intelligence.provider import (
    InMemorySalesProvider,
    WebhookSalesProvider,
)
from agentforge.use_cases.sales_intelligence.tools import build_sales_tools
from agentforge.use_cases.sales_intelligence.workflow import (
    WORKFLOW_ID,
    sales_intelligence_workflow,
)

if TYPE_CHECKING:
    from agentforge.agents.tools import ToolRegistry

__all__ = [
    "COMPENSATOR_TYPE",
    "DEFAULT_ICP_PATH",
    "WORKFLOW_ID",
    "CopywritingAgent",
    "DispatchReceipt",
    "ICPProfile",
    "InMemorySalesProvider",
    "Lead",
    "LeadScore",
    "OutreachAgent",
    "OutreachCompensator",
    "OutreachDraft",
    "OutreachPackage",
    "ResearchAgent",
    "ResearchDossier",
    "ScoringAgent",
    "WebhookSalesProvider",
    "build_sales_tools",
    "default_icp",
    "load_icp",
    "register_sales_intelligence",
    "sales_intelligence_workflow",
]


def register_sales_intelligence(
    registry: object,
    *,
    tools: ToolRegistry | None = None,
    icp: ICPProfile | None = None,
    provider_name: str = "sales",
) -> None:
    """Register the four Sales Intelligence agents and the send compensator on a
    :class:`StepRegistry`."""
    from agentforge.core.runners import StepRegistry

    assert isinstance(registry, StepRegistry)
    registry.register(ResearchAgent.agent_type, ResearchAgent(tools))
    registry.register(ScoringAgent.agent_type, ScoringAgent(icp))
    registry.register(CopywritingAgent.agent_type, CopywritingAgent())
    registry.register(OutreachAgent.agent_type, OutreachAgent(provider_name=provider_name))
    registry.register(COMPENSATOR_TYPE, OutreachCompensator())
