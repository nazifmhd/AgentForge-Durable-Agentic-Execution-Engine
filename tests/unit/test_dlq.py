from __future__ import annotations

from datetime import UTC, datetime

from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    seed_instance,
)
from tests.factories import StreamBuilder, linear_workflow, make_step

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import OnFailure, StepStatus, WorkflowStatus
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.events import fold
from agentforge.core.events import types as E
from agentforge.core.executor import StepExecutor
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult

T0 = datetime(2026, 8, 1, tzinfo=UTC)
TENANT = "tenant-1"


def test_fold_requeue_resets_instance_and_step() -> None:
    b = StreamBuilder(instance_id="i", tenant_id=TENANT, clock=T0)
    b.created(workflow_id="wf-linear", workflow_version="1.0.0")
    b.wf_status("pending", "running")
    b.step_status("step_1", "pending", "ready")
    b.step_started("step_1", worker_id="w")
    b.step_failed("step_1", error_type="ValueError", error_message="x", retryable=False)
    b.wf_status("running", "dead_lettered", reason="boom")
    inst = fold(b.events, definition=linear_workflow(2))
    assert inst.status is WorkflowStatus.DEAD_LETTERED

    b.raw(E.WorkflowRequeued, step_id="step_1", dlq_id=7)
    inst = fold(b.events, definition=linear_workflow(2))
    assert inst.status is WorkflowStatus.RUNNING
    assert inst.step("step_1").status is StepStatus.READY
    assert inst.step("step_1").attempts == 0
    assert inst.step("step_1").error_type is None


async def test_requeued_instance_is_reclaimed_and_completes() -> None:
    calls = {"n": 0}

    async def once_bad(ctx: StepContext) -> StepResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("permanent, first time")
        return StepResult(output={"ok": True})

    reg = StepRegistry()
    reg.register("executor_agent", FunctionRunner(once_bad))

    journal = InMemoryJournal()
    leases = InMemoryLeaseStore(journal)
    defs = InMemoryDefinitions()
    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        on_failure=OnFailure.DEAD_LETTER,
        steps=(make_step("a"),),
    )
    defs.add(wf)
    driver = WorkflowDriver(
        journal,
        defs,
        StepExecutor(reg),
        FakeDeadLetters(),  # type: ignore[arg-type]
        clock=FixedClock(T0),
        ids=SequentialIdGenerator("ev"),
    )
    await seed_instance(journal, wf)

    lease = (await leases.acquire_runnable("w1", 5, T0))[0]
    r = await driver.drive(lease, leases.make_guard(lease))
    await leases.release("w1", "inst-1")
    assert r.result is DriveResult.DEAD_LETTERED

    # operator requeues: append WorkflowRequeued directly (no lease needed)
    instance = await journal.get_instance("inst-1", TENANT, definition=wf)
    await journal.append_new(
        "inst-1",
        TENANT,
        [
            E.WorkflowRequeued(
                event_id="rq",
                instance_id="inst-1",
                tenant_id=TENANT,
                sequence=1,
                occurred_at=T0,
                step_id="a",
            )
        ],
        expected_version=instance.version,
    )
    assert journal.index["inst-1"].status == "running"  # now claimable again

    lease2 = (await leases.acquire_runnable("w2", 5, T0))[0]
    r2 = await driver.drive(lease2, leases.make_guard(lease2))
    assert r2.result is DriveResult.COMPLETED
    final = await journal.get_instance("inst-1", TENANT, definition=wf)
    assert final.status is WorkflowStatus.COMPLETED
    assert calls["n"] == 2
