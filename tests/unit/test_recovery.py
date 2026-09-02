"""Crash / recovery: a worker dies mid-step; another finishes the job with no
duplicated step completion."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest
from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    seed_instance,
)
from tests.factories import StreamBuilder, linear_workflow

from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.events import types as E
from agentforge.core.executor import StepExecutor
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.exceptions import LeaseLostError

T0 = datetime(2026, 6, 1, tzinfo=UTC)
TENANT = "tenant-1"


def _driver(journal, defs, registry, clock):
    return WorkflowDriver(
        journal,
        defs,
        StepExecutor(registry),
        FakeDeadLetters(),  # type: ignore[arg-type]
        clock=clock,
        ids=SequentialIdGenerator("ev"),
    )


async def test_reset_and_complete_from_crashed_stream() -> None:
    journal = InMemoryJournal()
    leases = InMemoryLeaseStore(journal)
    defs = InMemoryDefinitions()
    defs.add(linear_workflow(2))

    reg = StepRegistry()

    async def ok(ctx: StepContext) -> StepResult:
        return StepResult(output={"ok": ctx.step_id})

    reg.register("executor_agent", FunctionRunner(ok))

    # a worker that died right after persisting StepStarted for step_1
    b = StreamBuilder(instance_id="inst-1", tenant_id=TENANT, clock=T0)
    b.created(workflow_id="wf-linear", workflow_version="1.0.0")
    b.wf_status("pending", "running")
    b.step_status("step_1", "pending", "ready")
    b.step_started("step_1", attempt=1, worker_id="dead-worker")
    await journal.append_new("inst-1", TENANT, b.events, expected_version=0)

    driver = _driver(journal, defs, reg, FixedClock(T0))
    lease = (await leases.acquire_runnable("live-worker", 5, T0))[0]
    report = await driver.drive(lease, leases.make_guard(lease))

    assert report.result is DriveResult.COMPLETED
    events = journal._events["inst-1"]
    resets = [
        e
        for e in events
        if isinstance(e, E.StepStatusChanged)
        and e.to_status.value == "ready"
        and e.reason == "worker-recovered"
    ]
    assert len(resets) == 1
    completions = [e for e in events if isinstance(e, E.StepCompleted) and e.step_id == "step_1"]
    assert len(completions) == 1  # step ran once to completion, not twice


async def test_cancel_mid_step_then_recover() -> None:
    journal = InMemoryJournal()
    leases = InMemoryLeaseStore(journal)
    defs = InMemoryDefinitions()
    defs.add(linear_workflow(1))

    release = asyncio.Event()
    entered = asyncio.Event()
    calls = {"n": 0}

    async def slow(ctx: StepContext) -> StepResult:
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            await release.wait()  # hang until the test "kills" the worker
        return StepResult(output={"ok": True})

    reg = StepRegistry()
    reg.register("executor_agent", FunctionRunner(slow))

    await seed_instance(journal, linear_workflow(1))
    driver = _driver(journal, defs, reg, FixedClock(T0))

    lease1 = (await leases.acquire_runnable("worker-1", 5, T0))[0]
    task = asyncio.create_task(driver.drive(lease1, leases.make_guard(lease1)))
    await asyncio.wait_for(entered.wait(), timeout=2)

    # StepStarted is durable; the attempt is not done
    mid = await journal.get_instance("inst-1", TENANT, definition=linear_workflow(1))
    assert mid.step("step_1").status.value == "running"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    leases.expire_all()  # worker-1 is gone

    driver2 = _driver(journal, defs, reg, FixedClock(T0))
    lease2 = (await leases.acquire_runnable("worker-2", 5, T0))[0]
    release.set()
    report = await driver2.drive(lease2, leases.make_guard(lease2))

    assert report.result is DriveResult.COMPLETED
    final = await journal.get_instance("inst-1", TENANT, definition=linear_workflow(1))
    assert final.status.value == "completed"
    assert calls["n"] == 2  # first attempt cancelled, second completed


async def test_stale_worker_cannot_write_after_lease_stolen() -> None:
    journal = InMemoryJournal()
    leases = InMemoryLeaseStore(journal)
    defs = InMemoryDefinitions()
    defs.add(linear_workflow(2))

    async def noop(ctx: StepContext) -> StepResult:
        return StepResult(output={})

    reg = StepRegistry()
    reg.register("executor_agent", FunctionRunner(noop))

    await seed_instance(journal, linear_workflow(2))

    lease_old = (await leases.acquire_runnable("worker-1", 5, T0))[0]
    leases.expire_all()
    lease_new = (await leases.acquire_runnable("worker-2", 5, T0))[0]
    assert lease_new.fence_token == lease_old.fence_token + 1

    guard_old = leases.make_guard(lease_old)
    with pytest.raises(LeaseLostError):
        await journal.append_new(
            "inst-1",
            TENANT,
            [
                E.InstanceStatusChanged(
                    event_id="x",
                    instance_id="inst-1",
                    tenant_id=TENANT,
                    sequence=1,
                    occurred_at=T0,
                    from_status="pending",
                    to_status="running",
                )
            ],
            expected_version=1,
            guard=guard_old,
        )
