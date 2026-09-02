from __future__ import annotations

from datetime import UTC, datetime

from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    RecordingActionProvider,
    seed_instance,
)
from tests.factories import StreamBuilder, make_step

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import OnFailure, StepStatus, WorkflowStatus
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.events import types as E
from agentforge.core.executor import StepExecutor
from agentforge.core.outbox import InMemoryOutboxStore
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.core.side_effects import SideEffectGuard
from agentforge.integrations.actions.base import ProviderRegistry

T0 = datetime(2026, 7, 1, tzinfo=UTC)
TENANT = "tenant-1"


class _Rig:
    def __init__(self, registry: StepRegistry, provider: RecordingActionProvider) -> None:
        self.journal = InMemoryJournal()
        self.leases = InMemoryLeaseStore(self.journal)
        self.defs = InMemoryDefinitions()
        self.provider = provider
        providers = ProviderRegistry()
        providers.register(provider)
        self.guard = SideEffectGuard(InMemoryOutboxStore(), providers, clock=FixedClock(T0))
        self.driver = WorkflowDriver(
            self.journal,
            self.defs,
            StepExecutor(registry),
            FakeDeadLetters(),  # type: ignore[arg-type]
            side_effects=self.guard,
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


async def test_rollback_compensates_side_effects_and_steps() -> None:
    provider = RecordingActionProvider(idempotent={"create_vm"})
    reg = StepRegistry()

    async def provision(ctx: StepContext) -> StepResult:
        await ctx.execute_effect("create_vm", {"size": "m"}, provider="test")
        return StepResult(output={"vm": "vm-1"})

    async def configure(ctx: StepContext) -> StepResult:
        raise ValueError("bad config, permanent")

    undo_calls: list[dict] = []

    async def undo_provision(ctx: StepContext) -> StepResult:
        undo_calls.append(ctx.inputs)
        return StepResult(output={"undone": True})

    reg.register("executor_agent", FunctionRunner(provision))
    reg.register("configure_agent", FunctionRunner(configure))
    reg.register("undo_provision", FunctionRunner(undo_provision))

    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        on_failure=OnFailure.ROLLBACK,
        steps=(
            make_step(
                "provision",
                side_effects=("create_vm",),
                compensation_action="undo_provision",
            ),
            make_step("configure", ("provision",), agent_type="configure_agent"),
        ),
    )
    rig = _Rig(reg, provider)
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)

    assert await rig.drive() is DriveResult.ROLLED_BACK

    inst = await rig.instance()
    assert inst.status is WorkflowStatus.ROLLED_BACK
    assert inst.step("provision").status is StepStatus.COMPENSATED
    assert [r.effect_name for r in provider.compensated] == ["create_vm"]
    assert undo_calls == [{"output": {"vm": "vm-1"}}]

    events = rig.journal._events["inst-1"]
    assert any(isinstance(e, E.SideEffectCompensated) for e in events)
    assert any(isinstance(e, E.StepCompensated) and e.step_id == "provision" for e in events)


async def test_side_effect_fires_once_across_a_crash() -> None:
    """Step re-runs on recovery; the guard makes the external effect exactly-once."""
    provider = RecordingActionProvider(idempotent={"send"})
    reg = StepRegistry()

    async def send_step(ctx: StepContext) -> StepResult:
        await ctx.execute_effect("send", {"to": "x"}, provider="test")
        return StepResult(output={"sent": True})

    async def plain(ctx: StepContext) -> StepResult:
        return StepResult(output={})

    reg.register("executor_agent", FunctionRunner(send_step))
    reg.register("plain_agent", FunctionRunner(plain))

    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        steps=(
            make_step("notify", side_effects=("send",)),
            make_step("after", ("notify",), agent_type="plain_agent"),
        ),
    )
    rig = _Rig(reg, provider)
    rig.defs.add(wf)

    # crashed mid-step: StepStarted persisted, no completion
    b = StreamBuilder(instance_id="inst-1", tenant_id=TENANT, clock=T0)
    b.created(workflow_id="w", workflow_version="1.0.0")
    b.wf_status("pending", "running")
    b.step_status("notify", "pending", "ready")
    b.step_started("notify", worker_id="dead")
    await rig.journal.append_new("inst-1", TENANT, b.events, expected_version=0)

    # first attempt actually ran the effect before the "crash"
    await rig.guard.execute(
        instance_id="inst-1",
        tenant_id=TENANT,
        step_id="notify",
        effect_name="send",
        params={"to": "x"},
        provider_name="test",
    )
    assert len(provider.executed) == 1

    assert await rig.drive() is DriveResult.COMPLETED
    assert len(provider.executed) == 1  # recovery re-ran the step but NOT the effect

    inst = await rig.instance()
    effects = await rig.guard.list_effects("inst-1", TENANT)
    assert [e.status for e in effects] == ["executed"]
    assert inst.status is WorkflowStatus.COMPLETED


async def test_compensation_failure_escalates_for_human() -> None:
    provider = RecordingActionProvider(idempotent={"create_vm"}, compensate_fails={"create_vm"})
    reg = StepRegistry()

    async def provision(ctx: StepContext) -> StepResult:
        await ctx.execute_effect("create_vm", {}, provider="test")
        return StepResult(output={})

    async def configure(ctx: StepContext) -> StepResult:
        raise ValueError("permanent")

    reg.register("executor_agent", FunctionRunner(provision))
    reg.register("configure_agent", FunctionRunner(configure))

    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        on_failure=OnFailure.ROLLBACK,
        steps=(
            make_step("provision", side_effects=("create_vm",)),
            make_step("configure", ("provision",), agent_type="configure_agent"),
        ),
    )
    rig = _Rig(reg, provider)
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)

    assert await rig.drive() is DriveResult.WAITING_APPROVAL
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.WAITING_APPROVAL
    assert inst.escalations[-1].reason == "compensation_failed"
