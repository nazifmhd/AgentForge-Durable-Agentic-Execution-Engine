"""Phase 8 — the Sales Intelligence & Outreach reference workflow."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import yaml
from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryEscalationReadStore,
    InMemoryJournal,
    InMemoryLeaseStore,
    ScriptedLLMProvider,
    seed_instance,
)

from agentforge.core.cost.registry import ModelRegistry
from agentforge.core.cost.router import CostAwareRouter
from agentforge.core.domain.enums import StepStatus, WorkflowStatus
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.escalation import EscalationController
from agentforge.core.executor import StepExecutor
from agentforge.core.llm_client import LLMClient
from agentforge.core.outbox import InMemoryOutboxStore
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import StepContext, StepRegistry
from agentforge.core.side_effects import SideEffectGuard
from agentforge.integrations.actions.base import EffectRequest, EffectResult, ProviderRegistry
from agentforge.integrations.llm.base import LLMProviderRegistry
from agentforge.use_cases.sales_intelligence import (
    InMemorySalesProvider,
    Lead,
    ScoringAgent,
    build_sales_tools,
    load_icp,
    register_sales_intelligence,
    sales_intelligence_workflow,
)
from agentforge.use_cases.sales_intelligence.agents.copywriting_agent import CopywritingAgent
from agentforge.use_cases.sales_intelligence.agents.research_agent import ResearchAgent
from agentforge.use_cases.sales_intelligence.config import default_icp
from agentforge.use_cases.sales_intelligence.models import ICPProfile
from agentforge.use_cases.sales_intelligence.provider import SEND_EMAIL

T0 = datetime(2026, 9, 1, tzinfo=UTC)
TENANT = "tenant-1"

_YAML = """
models:
  m:
    model_id: m-1
    provider: p
    input_per_mtok: 1.0
    output_per_mtok: 4.0
    context_window: 100000
    max_output_tokens: 8192
    avg_latency_ms: 300
    reliability_score: 0.95
    tiers: [cheap, standard, premium]
fallback_chains:
  cheap: [m]
  standard: [m]
  premium: [m]
"""

_LEAD = {
    "company_name": "Acme Analytics",
    "domain": "acme.example",
    "contact_name": "Dana Lee",
    "contact_title": "VP Engineering",
    "contact_email": "dana@acme.example",
    "source": "webinar",
}

_DOSSIER = json.dumps(
    {
        "company_summary": "Acme sells a cloud analytics platform to mid-market retailers.",
        "industry": "software",
        "headcount_estimate": 280,
        "tech_stack": ["kubernetes", "snowflake"],
        "buying_signals": ["hiring 3 platform engineers", "Series B in March"],
        "recent_news": ["Raised $40M Series B"],
        "pain_hypotheses": ["scaling ingestion pipeline"],
        "sources": ["acme.example/about", "news wire"],
        "confidence": 0.72,
    }
)

_SCORE_HOT = json.dumps(
    {
        "fit_score": 84,
        "rationale": "SaaS, 280 staff, hiring platform engineers, fresh funding.",
        "matched_criteria": ["industry", "headcount", "hiring signal", "funding signal"],
        "missing_criteria": [],
        "disqualifier_hits": [],
        "recommended_action": "send first-touch email",
    }
)

_SCORE_DISQUALIFIED = json.dumps(
    {
        "fit_score": 40,
        "rationale": "Already an active customer.",
        "matched_criteria": ["industry"],
        "missing_criteria": ["not a prospect"],
        "disqualifier_hits": ["existing customer"],
        "recommended_action": "route to account management",
    }
)

_DRAFTS = json.dumps(
    {
        "email": {
            "subject": "Ingestion scaling at Acme",
            "body": "Saw you're hiring three platform engineers after the Series B. "
            "Most teams at that stage hit an ingestion ceiling before headcount catches up. "
            "We help with exactly that. Worth a short call?",
            "call_to_action": "Open to 15 minutes next week?",
            "personalization_notes": ["hiring platform engineers", "Series B"],
        },
        "linkedin": {
            "body": "Dana - noticed Acme is scaling the platform team post-Series B. "
            "We work with teams hitting ingestion limits at that point. Happy to share notes.",
            "call_to_action": "Connect?",
            "personalization_notes": ["Series B"],
        },
    }
)

_REVIEW_CLEAN = json.dumps({"issues": []})

_FULL_SCRIPT = [_DOSSIER, _SCORE_HOT, _DRAFTS, _REVIEW_CLEAN]


class _Rig:
    def __init__(
        self,
        *,
        script: list[str],
        icp: ICPProfile | None = None,
        provider: InMemorySalesProvider | None = None,
        require_send_approval: bool = False,
    ) -> None:
        self.journal = InMemoryJournal()
        self.leases = InMemoryLeaseStore(self.journal)
        self.defs = InMemoryDefinitions()
        self.clock = FixedClock(T0)

        models = ModelRegistry.from_dict(yaml.safe_load(_YAML))
        providers = LLMProviderRegistry()
        providers.register(ScriptedLLMProvider(script))
        llm = LLMClient(CostAwareRouter(models), models, providers)

        self.sales = provider or InMemorySalesProvider()
        action_providers = ProviderRegistry()
        action_providers.register(self.sales)
        self.guard = SideEffectGuard(InMemoryOutboxStore(), action_providers, clock=self.clock)

        self.registry = StepRegistry()
        register_sales_intelligence(self.registry, icp=icp or default_icp())

        self.driver = WorkflowDriver(
            self.journal,
            self.defs,
            StepExecutor(self.registry),
            FakeDeadLetters(),  # type: ignore[arg-type]
            side_effects=self.guard,
            llm=llm,
            clock=self.clock,
            ids=SequentialIdGenerator("ev"),
        )
        self.controller = EscalationController(
            InMemoryEscalationReadStore(self.journal),
            self.journal,
            clock=self.clock,
            ids=SequentialIdGenerator("re"),
        )
        self.wf = sales_intelligence_workflow(require_send_approval=require_send_approval)
        self.defs.add(self.wf)

    async def seed(self, context: dict[str, Any] | None = None) -> None:
        await seed_instance(self.journal, self.wf, context=context or {"lead": _LEAD})

    async def drive(self) -> DriveResult:
        lease = (await self.leases.acquire_runnable("w1", 5, self.clock.now()))[0]
        report = await self.driver.drive(lease, self.leases.make_guard(lease))
        await self.leases.release("w1", lease.instance_id)
        return report.result

    async def instance(self) -> Any:
        return await self.journal.get_instance("inst-1", TENANT, definition=self.wf)


# --- agent-level -------------------------------------------------------


def _ctx(script: list[str], inputs: dict[str, Any], *, agent_type: str) -> StepContext:
    models = ModelRegistry.from_dict(yaml.safe_load(_YAML))
    providers = LLMProviderRegistry()
    providers.register(ScriptedLLMProvider(script))
    return StepContext(
        instance_id="inst-1",
        tenant_id=TENANT,
        step_id="s1",
        agent_type=agent_type,
        attempt=1,
        inputs=inputs,
        instance_context={},
        clock=FixedClock(T0),
        llm_client=LLMClient(CostAwareRouter(models), models, providers),
        guard=None,
    )


async def test_research_agent_builds_a_dossier_from_tools_and_llm() -> None:
    seen: list[dict[str, Any]] = []

    async def crm(args: dict[str, Any]) -> dict[str, Any]:
        seen.append(args)
        return {"found": True, "owner": "AE Jones"}

    tools = build_sales_tools(crm_lookup=crm)
    ctx = _ctx([_DOSSIER], {"lead": _LEAD}, agent_type="sales_research_agent")

    result = await ResearchAgent(tools).run(ctx)

    assert seen and seen[0]["domain"] == "acme.example"
    assert result.output["dossier"]["industry"] == "software"
    assert result.output["dossier"]["already_in_crm"] is True
    assert result.output["lead"]["company_name"] == "Acme Analytics"


async def test_scoring_agent_derives_tier_from_icp_thresholds() -> None:
    icp = ICPProfile(name="t", hot_threshold=80, warm_threshold=50, qualify_threshold=25)
    ctx = _ctx(
        [_SCORE_HOT],
        {"research": {"lead": _LEAD, "dossier": json.loads(_DOSSIER)}},
        agent_type="sales_scoring_agent",
    )

    result = await ScoringAgent(icp).run(ctx)

    assert result.output["fit_score"] == 84
    assert result.output["tier"] == "hot"


async def test_scoring_agent_disqualifies_on_a_disqualifier_hit() -> None:
    ctx = _ctx(
        [_SCORE_DISQUALIFIED],
        {"research": {"lead": _LEAD, "dossier": json.loads(_DOSSIER)}},
        agent_type="sales_scoring_agent",
    )

    result = await ScoringAgent(default_icp()).run(ctx)

    assert result.output["tier"] == "disqualified"
    assert result.output["disqualifier_hits"] == ["existing customer"]


async def test_copywriting_agent_skips_a_disqualified_lead_without_spending() -> None:
    ctx = _ctx(
        [],  # no LLM response needed — it must not call the model
        {
            "research": {"lead": _LEAD, "dossier": json.loads(_DOSSIER)},
            "score": {"tier": "disqualified", "fit_score": 10},
        },
        agent_type="sales_copywriting_agent",
    )

    result = await CopywritingAgent().run(ctx)

    assert result.output == {"skipped": True, "reason": "lead disqualified"}
    assert ctx.charges == []


async def test_copywriting_agent_drafts_and_self_reviews() -> None:
    ctx = _ctx(
        [_DRAFTS, _REVIEW_CLEAN],
        {
            "research": {"lead": _LEAD, "dossier": json.loads(_DOSSIER)},
            "score": {"tier": "hot", "fit_score": 84, "rationale": "good fit"},
        },
        agent_type="sales_copywriting_agent",
    )

    result = await CopywritingAgent().run(ctx)

    assert result.output["skipped"] is False
    assert result.output["outreach"]["email"]["subject"] == "Ingestion scaling at Acme"
    assert result.output["outreach"]["linkedin"]["channel"] == "linkedin"
    assert result.output["outreach"]["revisions"] == 0


async def test_copywriting_agent_revises_when_the_reviewer_flags_issues() -> None:
    better = json.dumps(
        {
            "email": {
                "subject": "Shorter subject",
                "body": "Tighter body that references the Series B and hiring.",
                "call_to_action": "15 minutes?",
                "personalization_notes": ["Series B"],
            },
            "linkedin": json.loads(_DRAFTS)["linkedin"],
        }
    )
    ctx = _ctx(
        [_DRAFTS, json.dumps({"issues": ["subject too long", "weak CTA"]}), better],
        {
            "research": {"lead": _LEAD, "dossier": json.loads(_DOSSIER)},
            "score": {"tier": "warm", "fit_score": 60},
        },
        agent_type="sales_copywriting_agent",
    )

    result = await CopywritingAgent().run(ctx)

    assert result.output["outreach"]["revisions"] == 1
    assert result.output["outreach"]["email"]["subject"] == "Shorter subject"
    assert result.output["outreach"]["quality_issues"] == ["subject too long", "weak CTA"]


# --- workflow-level ---------------------------------------------------


async def test_full_workflow_researches_scores_drafts_and_sends() -> None:
    rig = _Rig(script=list(_FULL_SCRIPT))
    await rig.seed()

    assert await rig.drive() is DriveResult.COMPLETED

    inst = await rig.instance()
    assert inst.status is WorkflowStatus.COMPLETED
    assert inst.step_states["score"].output["tier"] == "hot"
    assert inst.step_states["send"].output["email_ref"] == "email-2"
    assert [e.params["to"] for e in rig.sales.emails] == ["dana@acme.example"]
    assert len(rig.sales.crm_tasks) == 1


async def test_disqualified_lead_short_circuits_drafting_and_sending() -> None:
    rig = _Rig(script=[_DOSSIER, _SCORE_DISQUALIFIED])
    await rig.seed()

    assert await rig.drive() is DriveResult.COMPLETED

    inst = await rig.instance()
    assert inst.step_states["draft_outreach"].output == {
        "skipped": True,
        "reason": "lead disqualified",
    }
    assert inst.step_states["send"].output["skipped"] is True
    assert rig.sales.emails == []
    assert rig.sales.crm_tasks == []


async def test_send_step_waits_for_approval_then_completes() -> None:
    rig = _Rig(script=list(_FULL_SCRIPT), require_send_approval=True)
    await rig.seed()

    assert await rig.drive() is DriveResult.WAITING_APPROVAL
    assert rig.sales.emails == []  # nothing sent yet

    pending = await rig.controller.list_pending(tenant_id=TENANT)
    assert [p.step_id for p in pending] == ["send"]
    await rig.controller.resolve(
        pending[0].escalation_id, tenant_id=TENANT, resolution="approve", resolved_by="rep"
    )

    assert await rig.drive() is DriveResult.COMPLETED
    assert len(rig.sales.emails) == 1


async def test_send_failure_rolls_back_the_crm_task() -> None:
    class _FailEmail(InMemorySalesProvider):
        async def execute(self, req: EffectRequest) -> EffectResult:
            if req.effect_name == SEND_EMAIL:
                raise RuntimeError("ESP rejected the message")
            return await super().execute(req)

    provider = _FailEmail()
    rig = _Rig(script=list(_FULL_SCRIPT), provider=provider)
    await rig.seed()

    assert await rig.drive() is DriveResult.ROLLED_BACK

    inst = await rig.instance()
    assert inst.status is WorkflowStatus.ROLLED_BACK
    assert inst.step_states["send"].status in (StepStatus.FAILED, StepStatus.COMPENSATED)
    assert len(provider.crm_tasks) == 1
    assert provider.crm_tasks[0].status == "cancelled"  # compensated
    assert provider.emails == []


# --- config ---------------------------------------------------------


async def test_load_icp_reads_the_shipped_profile() -> None:
    icp = load_icp()
    assert icp.name == "mid-market-saas"
    assert "software" in icp.target_industries
    assert icp.hot_threshold == 75


def test_lead_from_context_accepts_flat_or_nested() -> None:
    assert Lead.from_context({"lead": _LEAD}).company_name == "Acme Analytics"
    assert Lead.from_context(_LEAD).contact_email == "dana@acme.example"
    assert Lead.from_context({"company_name": "X"}).domain == ""
