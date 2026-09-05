"""Deterministic replay (ADR-0005): a step that re-runs after a crash replays its
recorded LLM responses from the event log instead of re-calling the provider, so
recovery does not re-bill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import yaml
from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    ScriptedLLMProvider,
    seed_instance,
)
from tests.factories import StreamBuilder, make_step

from agentforge.core.cost.registry import ModelRegistry
from agentforge.core.cost.router import CostAwareRouter
from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import StepStatus
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
fallback_chains: {cheap: [m], standard: [m], premium: [m]}
"""


def _wf() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="w", name="w", version="1.0.0", steps=(make_step("think"),)
    )


def _rig(provider: ScriptedLLMProvider):
    journal = InMemoryJournal()
    leases = InMemoryLeaseStore(journal)
    defs = InMemoryDefinitions()
    defs.add(_wf())
    models = ModelRegistry.from_dict(yaml.safe_load(_YAML))
    providers = LLMProviderRegistry()
    providers.register(provider)
    llm = LLMClient(CostAwareRouter(models), models, providers)

    reg = StepRegistry()

    async def think(ctx: StepContext) -> StepResult:
        r = await ctx.llm([LLMMessage(role="user", content="think about it")], tier="standard")
        return StepResult(output={"said": r.text})

    reg.register("executor_agent", FunctionRunner(think))

    driver = WorkflowDriver(
        journal,
        defs,
        StepExecutor(reg),
        FakeDeadLetters(),  # type: ignore[arg-type]
        llm=llm,
        clock=FixedClock(T0),
        ids=SequentialIdGenerator("ev"),
    )
    return journal, leases, defs, driver


async def _drive(leases, driver) -> DriveResult:
    lease = (await leases.acquire_runnable("w1", 5, T0))[0]
    report = await driver.drive(lease, leases.make_guard(lease))
    await leases.release("w1", lease.instance_id)
    return report.result


async def test_happy_path_records_the_llm_call() -> None:
    provider = ScriptedLLMProvider(["the recorded answer"])
    journal, leases, _defs, driver = _rig(provider)
    await seed_instance(journal, _wf(), budget_limit_usd=10.0)

    assert await _drive(leases, driver) is DriveResult.COMPLETED

    recorded = [e for e in journal._events["inst-1"] if isinstance(e, E.LLMCallRecorded)]
    assert len(recorded) == 1
    assert recorded[0].response["text"] == "the recorded answer"
    assert recorded[0].model == "m-1"

    inst = await journal.get_instance("inst-1", TENANT, definition=_wf())
    assert inst.step_states["think"].recorded_llm_calls[0]["response"]["text"] == (
        "the recorded answer"
    )


async def test_recovery_replays_the_recorded_response_without_recalling() -> None:
    # A worker died after recording the LLM call but before completing the step.
    provider = ScriptedLLMProvider(["SHOULD NOT BE CALLED"])
    journal, leases, _defs, driver = _rig(provider)

    b = StreamBuilder(instance_id="inst-1", tenant_id=TENANT, clock=T0)
    b.created(workflow_id="w", workflow_version="1.0.0")
    b.wf_status("pending", "running")
    b.step_status("think", "pending", "ready")
    b.step_started("think", attempt=1, worker_id="dead")
    b.raw(
        E.LLMCallRecorded,
        step_id="think",
        attempt=1,
        request_digest="d0",
        model="m-1",
        response={
            "model_id": "m-1",
            "text": "the answer from before the crash",
            "tokens_input": 5,
            "tokens_output": 7,
            "stop_reason": "end_turn",
            "tool_calls": [],
        },
        tokens_input=5,
        tokens_output=7,
        cost_usd=0.001,
    )
    await journal.append_new("inst-1", TENANT, b.events, expected_version=0)

    assert await _drive(leases, driver) is DriveResult.COMPLETED

    inst = await journal.get_instance("inst-1", TENANT, definition=_wf())
    assert inst.step_states["think"].status is StepStatus.COMPLETED
    assert inst.step_states["think"].output == {"said": "the answer from before the crash"}

    assert provider.calls == []  # the model was never hit on the recovery run
    recorded = [e for e in journal._events["inst-1"] if isinstance(e, E.LLMCallRecorded)]
    assert len(recorded) == 1  # no duplicate recording
    charges = [e for e in journal._events["inst-1"] if isinstance(e, E.CostCharged)]
    assert charges == []  # replayed call is not re-billed


async def test_retry_after_failure_clears_recordings_and_recalls() -> None:
    # think fails once, then succeeds — the retry must make a fresh (billed) call.
    provider = ScriptedLLMProvider(["fresh answer on the retry"])
    journal = InMemoryJournal()
    leases = InMemoryLeaseStore(journal)
    defs = InMemoryDefinitions()
    models = ModelRegistry.from_dict(yaml.safe_load(_YAML))
    providers = LLMProviderRegistry()
    providers.register(provider)
    llm = LLMClient(CostAwareRouter(models), models, providers)

    calls: list[int] = []
    reg = StepRegistry()

    async def flaky(ctx: StepContext) -> StepResult:
        calls.append(1)
        r = await ctx.llm([LLMMessage(role="user", content="go")], tier="standard")
        if len(calls) == 1:
            raise TimeoutError("transient")  # retryable
        return StepResult(output={"said": r.text})

    reg.register("executor_agent", FunctionRunner(flaky))
    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        steps=(make_step("think"),),  # default retry policy (max_retries=2)
    )
    defs.add(wf)
    clock = FixedClock(T0)
    driver = WorkflowDriver(
        journal,
        defs,
        StepExecutor(reg),
        FakeDeadLetters(),  # type: ignore[arg-type]
        llm=llm,
        clock=clock,
        ids=SequentialIdGenerator("ev"),
    )
    await seed_instance(journal, wf, budget_limit_usd=10.0)

    async def drive_now() -> None:
        lease = (await leases.acquire_runnable("w1", 5, clock.now()))[0]
        await driver.drive(lease, leases.make_guard(lease))
        await leases.release("w1", lease.instance_id)

    # first drive: fails, schedules a retry
    await drive_now()
    inst = await journal.get_instance("inst-1", TENANT, definition=wf)
    assert inst.step_states["think"].recorded_llm_calls == []  # cleared on failure

    # advance past the retry backoff and drive again
    clock.set(T0 + timedelta(seconds=30))
    await drive_now()
    inst = await journal.get_instance("inst-1", TENANT, definition=wf)
    assert inst.step_states["think"].output == {"said": "fresh answer on the retry"}
    assert len(provider.calls) == 2  # called once per attempt, not replayed
