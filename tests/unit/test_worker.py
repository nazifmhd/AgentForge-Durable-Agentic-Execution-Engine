from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    seed_instance,
)
from tests.factories import linear_workflow

from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.executor import StepExecutor
from agentforge.core.ports import SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.worker import Worker

TENANT = "tenant-1"


def _make(registry: StepRegistry) -> tuple[InMemoryJournal, InMemoryLeaseStore, WorkflowDriver]:
    journal = InMemoryJournal()
    leases = InMemoryLeaseStore(journal, lease_seconds=30)
    defs = InMemoryDefinitions()
    defs.add(linear_workflow(3))
    driver = WorkflowDriver(
        journal,
        defs,
        StepExecutor(registry),
        FakeDeadLetters(),  # type: ignore[arg-type]
        ids=SequentialIdGenerator("ev"),
    )
    return journal, leases, driver


def _echo_registry() -> StepRegistry:
    reg = StepRegistry()

    async def fn(ctx: StepContext) -> StepResult:
        return StepResult(output={"s": ctx.step_id})

    reg.register("executor_agent", FunctionRunner(fn))
    return reg


async def test_run_once_completes_runnable_instances() -> None:
    journal, leases, driver = _make(_echo_registry())
    await seed_instance(journal, linear_workflow(3), instance_id="a")
    await seed_instance(journal, linear_workflow(3), instance_id="b")

    worker = Worker(leases, driver, worker_id="w1", concurrency=4)
    results = await worker.run_once()

    assert results == [DriveResult.COMPLETED, DriveResult.COMPLETED]
    for iid in ("a", "b"):
        inst = await journal.get_instance(iid, TENANT)
        assert inst.status.value == "completed"


async def test_two_workers_do_not_double_drive() -> None:
    journal, leases, driver = _make(_echo_registry())
    await seed_instance(journal, linear_workflow(3), instance_id="only")

    w1 = Worker(leases, driver, worker_id="w1", concurrency=4)
    w2 = Worker(leases, driver, worker_id="w2", concurrency=4)

    now = datetime(2026, 1, 2, tzinfo=UTC)
    claimed1 = await leases.acquire_runnable("w1", 4, now)
    claimed2 = await leases.acquire_runnable("w2", 4, now)
    assert len(claimed1) == 1
    assert claimed2 == []  # w1 holds the only runnable instance's lease

    del w1, w2


async def test_run_loop_drains_and_stops() -> None:
    journal, leases, driver = _make(_echo_registry())
    worker = Worker(
        leases,
        driver,
        worker_id="w1",
        concurrency=4,
        poll_interval_seconds=0.02,
        heartbeat_seconds=0.05,
        recovery_interval_seconds=0.05,
    )
    run_task = asyncio.create_task(worker.run())

    await seed_instance(journal, linear_workflow(3), instance_id="live")

    async def _wait_done() -> None:
        while True:
            inst = await journal.get_instance("live", TENANT)
            if inst and inst.status.value == "completed":
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait_done(), timeout=5)
    worker.stop()
    await asyncio.wait_for(run_task, timeout=5)
    assert not run_task.cancelled()


async def test_retry_backoff_is_respected_by_claim_gate() -> None:
    calls = {"n": 0}

    async def flaky(ctx: StepContext) -> StepResult:
        calls["n"] += 1
        if calls["n"] < 3:
            from agentforge.exceptions import RateLimitError

            raise RateLimitError("boom")
        return StepResult(output={})

    reg = StepRegistry()
    reg.register("executor_agent", FunctionRunner(flaky))
    journal, leases, driver = _make(reg)
    await seed_instance(journal, linear_workflow(1), instance_id="r")

    worker = Worker(leases, driver, worker_id="w1", concurrency=2)

    # first pass: attempt 1 fails, instance parks RETRYING with a future wakeup
    r1 = await worker.run_once()
    assert r1 == [DriveResult.PARKED]
    entry = journal.index["r"]
    assert entry.status == "retrying"
    assert entry.next_wakeup_at is not None

    # claim gate honours the wakeup: not runnable before it, runnable at/after it
    before = entry.next_wakeup_at - timedelta(seconds=1)
    assert await leases.acquire_runnable("w1", 2, before) == []
    assert len(await leases.acquire_runnable("w1", 2, entry.next_wakeup_at)) == 1
