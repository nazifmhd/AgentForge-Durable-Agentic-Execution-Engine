from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryEscalationReadStore,
    InMemoryJournal,
    InMemoryLeaseStore,
    seed_instance,
)
from tests.factories import make_step

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import StepStatus, WorkflowStatus
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.escalation import EscalationController
from agentforge.core.events import types as E
from agentforge.core.executor import StepExecutor
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.exceptions import ConfigurationError

T0 = datetime(2026, 9, 5, tzinfo=UTC)
TENANT = "tenant-1"


class _Rig:
    def __init__(self, registry: StepRegistry, clock: FixedClock) -> None:
        self.journal = InMemoryJournal()
        self.leases = InMemoryLeaseStore(self.journal)
        self.defs = InMemoryDefinitions()
        self.clock = clock
        self.driver = WorkflowDriver(
            self.journal,
            self.defs,
            StepExecutor(registry),
            FakeDeadLetters(),  # type: ignore[arg-type]
            clock=clock,
            ids=SequentialIdGenerator("ev"),
        )
        self.controller = EscalationController(
            InMemoryEscalationReadStore(self.journal),
            self.journal,
            clock=clock,
            ids=SequentialIdGenerator("re"),
        )

    async def drive(self) -> DriveResult:
        lease = (await self.leases.acquire_runnable("w1", 5, self.clock.now()))[0]
        report = await self.driver.drive(lease, self.leases.make_guard(lease))
        await self.leases.release("w1", lease.instance_id)
        return report.result

    async def instance(self):
        return await self.journal.get_instance(
            "inst-1", TENANT, definition=next(iter(self.defs._by_key.values()))
        )


def _registry(step_fn=None) -> StepRegistry:
    reg = StepRegistry()

    async def default(ctx: StepContext) -> StepResult:
        return StepResult(output={"did": ctx.step_id})

    reg.register("executor_agent", FunctionRunner(step_fn or default))
    return reg


def _approval_wf(**step_kw) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        steps=(
            make_step("a"),
            make_step("b", ("a",), requires_approval=True, **step_kw),
        ),
    )


async def test_resolve_approve_resumes_and_completes() -> None:
    rig = _Rig(_registry(), FixedClock(T0))
    wf = _approval_wf()
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)

    assert await rig.drive() is DriveResult.WAITING_APPROVAL
    pending = await rig.controller.list_pending(tenant_id=TENANT)
    assert [p.step_id for p in pending] == ["b"]

    await rig.controller.resolve(
        pending[0].escalation_id, tenant_id=TENANT, resolution="approve", resolved_by="alice"
    )
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.RUNNING
    assert inst.step("b").status is StepStatus.READY

    assert await rig.drive() is DriveResult.COMPLETED


async def test_resolve_skip_marks_step_skipped() -> None:
    rig = _Rig(_registry(), FixedClock(T0))
    wf = _approval_wf()
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)
    await rig.drive()
    esc = (await rig.controller.list_pending(tenant_id=TENANT))[0]

    await rig.controller.resolve(
        esc.escalation_id, tenant_id=TENANT, resolution="skip", resolved_by="bob"
    )
    assert await rig.drive() is DriveResult.COMPLETED
    inst = await rig.instance()
    assert inst.step("b").status is StepStatus.SKIPPED


async def test_resolve_abort_fails_the_instance() -> None:
    rig = _Rig(_registry(), FixedClock(T0))
    wf = _approval_wf()
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)
    await rig.drive()
    esc = (await rig.controller.list_pending(tenant_id=TENANT))[0]

    await rig.controller.resolve(
        esc.escalation_id, tenant_id=TENANT, resolution="abort", resolved_by="carol"
    )
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.FAILED


async def test_resolve_with_budget_bump_adjusts_limit() -> None:
    rig = _Rig(_registry(), FixedClock(T0))
    wf = _approval_wf()
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf, budget_limit_usd=0.10)
    await rig.drive()
    esc = (await rig.controller.list_pending(tenant_id=TENANT))[0]

    await rig.controller.resolve(
        esc.escalation_id,
        tenant_id=TENANT,
        resolution="approve",
        resolved_by="dana",
        new_budget_usd=5.0,
    )
    inst = await rig.instance()
    assert inst.budget_limit_usd == pytest.approx(5.0)
    assert any(isinstance(e, E.WorkflowBudgetAdjusted) for e in rig.journal._events["inst-1"])


async def test_resolve_unknown_escalation_raises() -> None:
    rig = _Rig(_registry(), FixedClock(T0))
    with pytest.raises(ConfigurationError):
        await rig.controller.resolve(
            "nope", tenant_id=TENANT, resolution="approve", resolved_by="x"
        )


async def test_deadline_auto_action_fires_via_sweep() -> None:
    clock = FixedClock(T0)
    rig = _Rig(_registry(), clock)
    wf = _approval_wf(approval_timeout_seconds=60, approval_auto_action="approve")
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)

    assert await rig.drive() is DriveResult.WAITING_APPROVAL
    esc = (await rig.controller.list_pending(tenant_id=TENANT))[0]
    assert esc.deadline == T0 + timedelta(seconds=60)

    # before the deadline: nothing fires
    assert await rig.controller.expire_due(T0 + timedelta(seconds=30)) == []

    # after the deadline: auto-approve, then the worker finishes it
    clock.set(T0 + timedelta(seconds=120))
    fired = await rig.controller.expire_due(clock.now())
    assert fired == [esc.escalation_id]

    timed_out = [e for e in rig.journal._events["inst-1"] if isinstance(e, E.EscalationTimedOut)]
    assert timed_out[0].auto_action == "approve"

    assert await rig.drive() is DriveResult.COMPLETED
    inst = await rig.instance()
    assert inst.step("b").status is StepStatus.COMPLETED
