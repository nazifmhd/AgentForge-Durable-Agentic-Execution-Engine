"""Step-initiated escalation — an agent flags its own output for human review.

Fills the gap between ``requires_approval`` (static, gate *before* the step runs)
and ``cost_threshold`` (budget only): a step that finished but is not confident
in its result asks for sign-off, keeping its output, and the workflow parks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

T0 = datetime(2026, 9, 1, tzinfo=UTC)
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


def _wf() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        steps=(make_step("classify"), make_step("act", ("classify",), agent_type="downstream")),
    )


def _registry(*, confidence: float = 0.3) -> StepRegistry:
    reg = StepRegistry()
    ran: list[str] = []

    async def classify(ctx: StepContext) -> StepResult:
        ran.append("classify")
        if confidence < 0.5:
            ctx.request_review(
                reason="low_confidence",
                confidence=confidence,
                recommendation="model was unsure between A and B",
                timeout_seconds=3600,
            )
        return StepResult(output={"label": "A", "confidence": confidence})

    async def downstream(ctx: StepContext) -> StepResult:
        ran.append("act")
        return StepResult(output={"acted_on": ctx.inputs["classify"]["label"]})

    reg.register("executor_agent", FunctionRunner(classify))
    reg.register("downstream", FunctionRunner(downstream))
    reg._ran = ran  # type: ignore[attr-defined]
    return reg


async def test_low_confidence_step_parks_for_review_keeping_its_output() -> None:
    reg = _registry(confidence=0.3)
    rig = _Rig(reg, FixedClock(T0))
    rig.defs.add(_wf())
    await seed_instance(rig.journal, _wf())

    assert await rig.drive() is DriveResult.WAITING_APPROVAL

    inst = await rig.instance()
    assert inst.status is WorkflowStatus.WAITING_APPROVAL
    assert inst.step_states["classify"].status is StepStatus.WAITING_APPROVAL
    assert inst.step_states["classify"].output == {"label": "A", "confidence": 0.3}  # kept
    assert inst.step_states["act"].status is StepStatus.PENDING  # downstream held
    assert reg._ran == ["classify"]  # act did not run

    esc = inst.escalations[-1]
    assert esc.reason == "low_confidence" and not esc.resolved
    raised = [e for e in rig.journal._events["inst-1"] if isinstance(e, E.EscalationRaised)][-1]
    assert raised.confidence == 0.3
    assert raised.recommendation == "model was unsure between A and B"
    assert raised.deadline == T0 + timedelta(seconds=3600)


async def test_approve_keeps_output_and_lets_the_workflow_finish() -> None:
    reg = _registry(confidence=0.3)
    rig = _Rig(reg, FixedClock(T0))
    rig.defs.add(_wf())
    await seed_instance(rig.journal, _wf())
    await rig.drive()

    esc = (await rig.controller.list_pending(tenant_id=TENANT))[0]
    await rig.controller.resolve(
        esc.escalation_id, tenant_id=TENANT, resolution="approve", resolved_by="analyst"
    )

    inst = await rig.instance()
    assert inst.step_states["classify"].status is StepStatus.COMPLETED  # not re-run
    assert reg._ran == ["classify"]

    assert await rig.drive() is DriveResult.COMPLETED
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.COMPLETED
    assert inst.step_states["act"].output == {"acted_on": "A"}
    assert reg._ran == ["classify", "act"]  # ran exactly once each


async def test_skip_drops_the_flagged_output() -> None:
    reg = _registry(confidence=0.3)
    rig = _Rig(reg, FixedClock(T0))
    rig.defs.add(_wf())
    await seed_instance(rig.journal, _wf())
    await rig.drive()

    esc = (await rig.controller.list_pending(tenant_id=TENANT))[0]
    await rig.controller.resolve(
        esc.escalation_id, tenant_id=TENANT, resolution="skip", resolved_by="analyst"
    )

    assert await rig.drive() is DriveResult.COMPLETED
    inst = await rig.instance()
    assert inst.step_states["classify"].status is StepStatus.SKIPPED
    assert inst.step_states["act"].status is StepStatus.COMPLETED  # dependents still run


async def test_abort_fails_the_instance() -> None:
    reg = _registry(confidence=0.3)
    rig = _Rig(reg, FixedClock(T0))
    rig.defs.add(_wf())
    await seed_instance(rig.journal, _wf())
    await rig.drive()

    esc = (await rig.controller.list_pending(tenant_id=TENANT))[0]
    await rig.controller.resolve(
        esc.escalation_id, tenant_id=TENANT, resolution="abort", resolved_by="analyst"
    )
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.FAILED


async def test_confident_step_does_not_escalate() -> None:
    reg = _registry(confidence=0.9)
    rig = _Rig(reg, FixedClock(T0))
    rig.defs.add(_wf())
    await seed_instance(rig.journal, _wf())

    assert await rig.drive() is DriveResult.COMPLETED
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.COMPLETED
    assert not inst.escalations
    assert reg._ran == ["classify", "act"]


async def test_review_deadline_auto_action_fires() -> None:
    clock = FixedClock(T0)
    reg = _registry(confidence=0.3)
    rig = _Rig(reg, clock)
    rig.defs.add(_wf())
    await seed_instance(rig.journal, _wf())
    await rig.drive()

    clock.set(T0 + timedelta(hours=2))  # past the 1h deadline
    fired = await rig.controller.expire_due(clock.now())
    assert len(fired) == 1

    inst = await rig.instance()
    # auto_action defaults to "abort"
    assert inst.status is WorkflowStatus.FAILED
