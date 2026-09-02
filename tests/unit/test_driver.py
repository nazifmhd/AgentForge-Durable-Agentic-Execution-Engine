from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    seed_instance,
)
from tests.factories import diamond_workflow, linear_workflow, make_step

from agentforge.core.domain.definition import RetryPolicy, WorkflowDefinition
from agentforge.core.domain.enums import OnFailure, StepStatus, WorkflowStatus
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.events import types as E
from agentforge.core.executor import StepExecutor
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult

T0 = datetime(2026, 6, 1, tzinfo=UTC)
TENANT = "tenant-1"


class _Rig:
    def __init__(self, registry: StepRegistry, clock: FixedClock) -> None:
        self.journal = InMemoryJournal()
        self.leases = InMemoryLeaseStore(self.journal, lease_seconds=30)
        self.defs = InMemoryDefinitions()
        self.dlq = FakeDeadLetters()
        self.clock = clock
        self.driver = WorkflowDriver(
            self.journal,
            self.defs,
            StepExecutor(registry),
            self.dlq,  # type: ignore[arg-type]
            clock=clock,
            ids=SequentialIdGenerator("ev"),
        )

    async def drive(self, worker_id: str = "w1") -> DriveResult:
        leases = await self.leases.acquire_runnable(worker_id, 10, self.clock.now())
        assert len(leases) == 1
        lease = leases[0]
        report = await self.driver.drive(lease, self.leases.make_guard(lease))
        await self.leases.release(worker_id, lease.instance_id)
        return report.result

    async def instance(self, instance_id: str = "inst-1"):
        defn = next(iter(self.defs._by_key.values()), None)
        return await self.journal.get_instance(instance_id, TENANT, definition=defn)

    def events(self, instance_id: str = "inst-1") -> list[E.BaseEvent]:
        return self.journal._events[instance_id]


def _registry(**fns) -> StepRegistry:
    reg = StepRegistry()

    async def default(ctx: StepContext) -> StepResult:
        return StepResult(output={"done": ctx.step_id, "attempt": ctx.attempt})

    reg.register("executor_agent", FunctionRunner(fns.pop("executor_agent", default)))
    for name, fn in fns.items():
        reg.register(name, FunctionRunner(fn))
    return reg


async def test_linear_workflow_runs_to_completion() -> None:
    rig = _Rig(_registry(), FixedClock(T0))
    rig.defs.add(linear_workflow(3))
    await seed_instance(rig.journal, linear_workflow(3))

    assert await rig.drive() is DriveResult.COMPLETED
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.COMPLETED
    assert [s.status for s in inst.step_states.values()] == [StepStatus.COMPLETED] * 3
    assert inst.context["_outputs"]["step_3"]["done"] == "step_3"


async def test_diamond_dispatches_b_and_c_in_one_wave() -> None:
    rig = _Rig(_registry(), FixedClock(T0))
    rig.defs.add(diamond_workflow())
    await seed_instance(rig.journal, diamond_workflow())

    assert await rig.drive() is DriveResult.COMPLETED
    start = {e.step_id: e.sequence for e in rig.events() if isinstance(e, E.StepStarted)}
    done = {e.step_id: e.sequence for e in rig.events() if isinstance(e, E.StepCompleted)}
    # b and c both start before either finishes -> one concurrent wave
    assert max(start["b"], start["c"]) < min(done["b"], done["c"])
    # d waits for both
    assert start["d"] > max(done["b"], done["c"])


async def test_max_concurrency_one_serializes_dispatch() -> None:
    rig = _Rig(_registry(), FixedClock(T0))
    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        max_concurrent_steps=1,
        steps=(make_step("a"), make_step("b"), make_step("c")),
    )
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)
    assert await rig.drive() is DriveResult.COMPLETED

    from itertools import pairwise

    starts = [e.sequence for e in rig.events() if isinstance(e, E.StepStarted)]
    completes = [e.sequence for e in rig.events() if isinstance(e, E.StepCompleted)]
    # each start is followed by its completion before the next start
    for s, nxt in pairwise(starts):
        assert any(s < c < nxt for c in completes)


async def test_retryable_failure_then_success() -> None:
    calls = {"n": 0}

    async def flaky(ctx: StepContext) -> StepResult:
        calls["n"] += 1
        if calls["n"] == 1:
            from agentforge.exceptions import RateLimitError

            raise RateLimitError("429")
        return StepResult(output={"ok": True})

    clock = FixedClock(T0)
    rig = _Rig(_registry(executor_agent=flaky), clock)
    wf = linear_workflow(1)
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)

    assert await rig.drive() is DriveResult.PARKED
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.RETRYING
    assert inst.step("step_1").status is StepStatus.FAILED
    assert inst.step("step_1").next_retry_at is not None

    clock.tick(120)
    assert await rig.drive() is DriveResult.COMPLETED
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.COMPLETED
    assert inst.step("step_1").attempts == 2
    assert calls["n"] == 2


async def test_retries_exhausted_pauses_by_default() -> None:
    async def always_fail(ctx: StepContext) -> StepResult:
        from agentforge.exceptions import RateLimitError

        raise RateLimitError("nope")

    clock = FixedClock(T0)
    rig = _Rig(_registry(executor_agent=always_fail), clock)
    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        on_failure=OnFailure.PAUSE,
        steps=(make_step("a", retry_policy=RetryPolicy(max_retries=1)),),
    )
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)

    assert await rig.drive() is DriveResult.PARKED  # attempt 1 failed, retry scheduled
    clock.tick(120)
    assert await rig.drive() is DriveResult.PAUSED  # attempt 2 failed, exhausted
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.PAUSED
    assert inst.step("a").attempts == 2


async def test_non_retryable_failure_dead_letters() -> None:
    async def boom(ctx: StepContext) -> StepResult:
        raise ValueError("permanent")

    rig = _Rig(_registry(executor_agent=boom), FixedClock(T0))
    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        on_failure=OnFailure.DEAD_LETTER,
        steps=(make_step("a"),),
    )
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)

    assert await rig.drive() is DriveResult.DEAD_LETTERED
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.DEAD_LETTERED
    assert rig.dlq.records[0]["step_id"] == "a"


async def test_requires_approval_parks_waiting() -> None:
    rig = _Rig(_registry(), FixedClock(T0))
    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        steps=(make_step("a"), make_step("b", ("a",), requires_approval=True)),
    )
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf)

    assert await rig.drive() is DriveResult.WAITING_APPROVAL
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.WAITING_APPROVAL
    assert inst.step("a").status is StepStatus.COMPLETED
    assert inst.step("b").status is StepStatus.WAITING_APPROVAL
    assert inst.escalations[0].step_id == "b"


async def test_budget_exhaustion_pauses() -> None:
    async def pricey(ctx: StepContext) -> StepResult:
        ctx.charge(0.60)
        return StepResult(output={})

    rig = _Rig(_registry(executor_agent=pricey), FixedClock(T0))
    wf = linear_workflow(3)
    rig.defs.add(wf)
    await seed_instance(rig.journal, wf, budget_limit_usd=0.50)

    assert await rig.drive() is DriveResult.PAUSED
    inst = await rig.instance()
    assert inst.status is WorkflowStatus.PAUSED
    assert inst.cost_accumulated_usd == pytest.approx(0.60)
    assert any(isinstance(e, E.BudgetExceeded) for e in rig.events())
    # only step_1 ran
    assert inst.step("step_1").status is StepStatus.COMPLETED
    assert inst.step("step_2").status is StepStatus.PENDING
