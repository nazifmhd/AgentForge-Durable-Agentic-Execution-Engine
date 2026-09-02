from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml
from tests.doubles import (
    FakeDeadLetters,
    FakeLLMProvider,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    seed_instance,
)
from tests.factories import make_step

from agentforge.core.cost.budget import BudgetService, InMemoryBudgetLedger
from agentforge.core.cost.registry import ModelRegistry
from agentforge.core.cost.router import CostAwareRouter
from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import WorkflowStatus
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.events import types as E
from agentforge.core.executor import StepExecutor
from agentforge.core.llm_client import LLMClient
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.integrations.llm.base import LLMMessage, LLMProviderRegistry

T0 = datetime(2026, 9, 1, tzinfo=UTC)
TENANT = "tenant-1"

_YAML = """
models:
  cheap:
    model_id: cheap-1
    provider: p
    input_per_mtok: 1.0
    output_per_mtok: 4.0
    context_window: 100000
    max_output_tokens: 8192
    avg_latency_ms: 300
    reliability_score: 0.95
    tiers: [standard]
fallback_chains:
  standard: [cheap]
"""


class _Rig:
    def __init__(
        self,
        registry: StepRegistry,
        *,
        provider: FakeLLMProvider,
        ledger: InMemoryBudgetLedger,
        daily_limit: float | None = 100.0,
    ) -> None:
        self.journal = InMemoryJournal()
        self.leases = InMemoryLeaseStore(self.journal)
        self.defs = InMemoryDefinitions()
        models = ModelRegistry.from_dict(yaml.safe_load(_YAML))
        providers = LLMProviderRegistry()
        providers.register(provider)
        llm = LLMClient(CostAwareRouter(models), models, providers)
        self.budget = BudgetService(ledger, tenant_daily_limit_usd=daily_limit)
        self.ledger = ledger
        self.driver = WorkflowDriver(
            self.journal,
            self.defs,
            StepExecutor(registry),
            FakeDeadLetters(),  # type: ignore[arg-type]
            llm=llm,
            budget=self.budget,
            clock=FixedClock(T0),
            ids=SequentialIdGenerator("ev"),
        )

    async def drive(self) -> DriveResult:
        lease = (await self.leases.acquire_runnable("w1", 5, T0))[0]
        report = await self.driver.drive(lease, self.leases.make_guard(lease))
        await self.leases.release("w1", lease.instance_id)
        return report.result

    async def instance(self):
        defn = next(iter(self.defs._by_key.values()))
        return await self.journal.get_instance("inst-1", TENANT, definition=defn)


async def test_step_llm_call_charges_cost_and_updates_tenant_ledger() -> None:
    provider = FakeLLMProvider(name="p", tokens_in=1_000_000, tokens_out=1_000_000)
    reg = StepRegistry()

    async def think(ctx: StepContext) -> StepResult:
        r = await ctx.llm([LLMMessage(role="user", content="plan it")], tier="standard")
        return StepResult(output={"said": r.text})

    reg.register("executor_agent", FunctionRunner(think))

    ledger = InMemoryBudgetLedger()
    rig = _Rig(reg, provider=provider, ledger=ledger)
    wf = WorkflowDefinition(workflow_id="w", name="w", version="1.0.0", steps=(make_step("a"),))
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf, budget_limit_usd=100.0)

    assert await rig.drive() is DriveResult.COMPLETED
    inst = await rig.instance()
    assert inst.cost_accumulated_usd == pytest.approx(5.0)  # 1M*1.0 + 1M*4.0
    assert inst.tokens_used.input == 1_000_000
    assert await ledger.spent_today(TENANT, T0.date()) == pytest.approx(5.0)

    cost_events = [e for e in rig.journal._events["inst-1"] if isinstance(e, E.CostCharged)]
    assert cost_events[0].model == "cheap-1"


async def test_preflight_budget_refusal_escalates_cost_threshold() -> None:
    provider = FakeLLMProvider(name="p", tokens_in=1_000_000, tokens_out=1_000_000)
    reg = StepRegistry()

    async def think(ctx: StepContext) -> StepResult:
        r = await ctx.llm(
            [LLMMessage(role="user", content="x")],
            tier="standard",
            expected_output_tokens=8_000,  # fits the model limits, busts the $0.01 budget
        )
        return StepResult(output={"said": r.text})

    reg.register("executor_agent", FunctionRunner(think))

    rig = _Rig(reg, provider=provider, ledger=InMemoryBudgetLedger())
    wf = WorkflowDefinition(workflow_id="w", name="w", version="1.0.0", steps=(make_step("a"),))
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf, budget_limit_usd=0.01)  # far too small

    assert await rig.drive() is DriveResult.WAITING_APPROVAL
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.WAITING_APPROVAL
    assert inst.escalations[-1].reason == "cost_threshold"
    assert provider.calls == []  # never dispatched — refused pre-flight
