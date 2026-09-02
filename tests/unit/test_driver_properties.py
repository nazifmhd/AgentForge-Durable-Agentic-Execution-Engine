"""Property: a worker may crash after *any single* durable append and another
worker still finishes the workflow, with every step completing exactly once.

(A crash is modelled as "process dies right after a commit" — the events are
durable, the driver never learns of them. We inject one such crash per run at a
varying append index and a varying number of total crashes, spaced out so the
run can still make forward progress between them.)
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    seed_instance,
)
from tests.factories import linear_workflow

from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.events import BaseEvent
from agentforge.core.events import types as E
from agentforge.core.executor import StepExecutor
from agentforge.core.leasing import Guard
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult

T0 = datetime(2026, 6, 1, tzinfo=UTC)
TENANT = "tenant-1"


class SimulatedCrash(RuntimeError):
    pass


class CrashingJournal(InMemoryJournal):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.fail_at: int | None = None

    async def append_new(
        self,
        instance_id: str,
        tenant_id: str,
        drafts: Sequence[BaseEvent],
        *,
        expected_version: int,
        guard: Guard | None = None,
        next_wakeup_at: datetime | None = None,
    ) -> tuple[int, list[BaseEvent]]:
        result = await super().append_new(
            instance_id,
            tenant_id,
            drafts,
            expected_version=expected_version,
            guard=guard,
            next_wakeup_at=next_wakeup_at,
        )
        self.calls += 1
        if self.fail_at is not None and self.calls >= self.fail_at:
            self.fail_at = None
            raise SimulatedCrash(f"crash after append #{self.calls}")
        return result


def _registry() -> StepRegistry:
    reg = StepRegistry()

    async def ok(ctx: StepContext) -> StepResult:
        return StepResult(output={"s": ctx.step_id, "att": ctx.attempt})

    reg.register("executor_agent", FunctionRunner(ok))
    return reg


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    n_steps=st.integers(min_value=1, max_value=4),
    first_crash=st.integers(min_value=1, max_value=12),
    crash_gap=st.integers(min_value=3, max_value=8),
    n_crashes=st.integers(min_value=1, max_value=4),
)
def test_crash_anywhere_still_completes_once(
    n_steps: int, first_crash: int, crash_gap: int, n_crashes: int
) -> None:
    asyncio.run(_scenario(n_steps, first_crash, crash_gap, n_crashes))


async def _scenario(n_steps: int, first_crash: int, crash_gap: int, n_crashes: int) -> None:
    journal = CrashingJournal()
    leases = InMemoryLeaseStore(journal, lease_seconds=30)
    defs = InMemoryDefinitions()
    defs.add(linear_workflow(n_steps))
    clock = FixedClock(T0)

    await seed_instance(journal, linear_workflow(n_steps))
    journal.fail_at = journal.calls + first_crash
    crashes_left = n_crashes

    result: DriveResult | None = None
    for life in range(200):
        driver = WorkflowDriver(
            journal,
            defs,
            StepExecutor(_registry()),
            FakeDeadLetters(),  # type: ignore[arg-type]
            clock=clock,
            ids=SequentialIdGenerator(f"ev{life}"),
        )
        snap = await journal.get_instance("inst-1", TENANT, definition=linear_workflow(n_steps))
        if snap and snap.status.value in ("completed", "rolled_back"):
            result = DriveResult.COMPLETED
            break
        if snap and snap.status.value in ("paused", "dead_lettered"):
            result = (
                DriveResult.PAUSED if snap.status.value == "paused" else DriveResult.DEAD_LETTERED
            )
            break

        leases.expire_all()
        claimed = await leases.acquire_runnable(f"w{life}", 5, clock.now())
        if not claimed:
            clock.tick(300)
            claimed = await leases.acquire_runnable(f"w{life}", 5, clock.now())
        assert claimed, "instance should always be re-claimable until terminal"
        lease = claimed[0]
        try:
            report = await driver.drive(lease, leases.make_guard(lease))
        except SimulatedCrash:
            crashes_left -= 1
            if crashes_left > 0:
                journal.fail_at = journal.calls + crash_gap
            continue
        result = report.result
        if result in (
            DriveResult.COMPLETED,
            DriveResult.PAUSED,
            DriveResult.DEAD_LETTERED,
        ):
            break
        if result == DriveResult.PARKED and report.next_wakeup_at:
            clock.set(report.next_wakeup_at + timedelta(seconds=1))
    else:  # pragma: no cover
        raise AssertionError("did not converge")

    assert result is DriveResult.COMPLETED
    final = await journal.get_instance("inst-1", TENANT, definition=linear_workflow(n_steps))
    assert final.status.value == "completed"

    events: list[BaseEvent] = journal._events["inst-1"]
    for i in range(1, n_steps + 1):
        completions = [
            e for e in events if isinstance(e, E.StepCompleted) and e.step_id == f"step_{i}"
        ]
        assert len(completions) == 1, f"step_{i} completed {len(completions)}x"
