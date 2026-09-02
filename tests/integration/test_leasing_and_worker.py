from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.factories import linear_workflow

from agentforge.core.dead_letter import DeadLetterService
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.executor import StepExecutor
from agentforge.core.instances import InstanceService
from agentforge.core.leasing import PgLeaseStore
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.exceptions import LeaseLostError
from agentforge.worker import Worker

pytestmark = pytest.mark.integration
TENANT = "tenant-1"


@pytest.fixture
def lease_store(sessionmaker) -> PgLeaseStore:
    return PgLeaseStore(sessionmaker, lease_seconds=30)


async def _seed(event_store, definitions, *, instance_id_hint: str = "x") -> str:
    await definitions.register(linear_workflow(2), tenant_id=TENANT)
    svc = InstanceService(
        event_store,
        definitions,
        clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
        ids=SequentialIdGenerator(instance_id_hint),
    )
    inst = await svc.create_instance(tenant_id=TENANT, workflow_id="wf-linear", version="1.0.0")
    return inst.instance_id


async def test_acquire_is_exclusive_and_skips_locked(event_store, definitions, lease_store) -> None:
    iid = await _seed(event_store, definitions)
    now = datetime.now(UTC)

    a = await lease_store.acquire_runnable("worker-a", 10, now)
    assert [x.instance_id for x in a] == [iid]

    b = await lease_store.acquire_runnable("worker-b", 10, now)
    assert b == []  # worker-a holds the lease, not yet expired


async def test_two_workers_partition_instances(event_store, definitions, lease_store) -> None:
    ids = {await _seed(event_store, definitions, instance_id_hint=f"p{i}") for i in range(6)}
    now = datetime.now(UTC)

    a = await lease_store.acquire_runnable("worker-a", 3, now)
    b = await lease_store.acquire_runnable("worker-b", 3, now)

    claimed = {x.instance_id for x in a} | {x.instance_id for x in b}
    assert claimed == ids
    assert not ({x.instance_id for x in a} & {x.instance_id for x in b})


async def test_heartbeat_extends_and_expiry_allows_reclaim(
    event_store, definitions, lease_store
) -> None:
    iid = await _seed(event_store, definitions)
    t0 = datetime.now(UTC)

    lease1 = (await lease_store.acquire_runnable("worker-a", 5, t0))[0]
    alive = await lease_store.heartbeat("worker-a", [iid], t0 + timedelta(seconds=5))
    assert alive == {iid}

    # still not reclaimable before expiry
    assert await lease_store.acquire_runnable("worker-b", 5, t0 + timedelta(seconds=10)) == []

    # after expiry, worker-b reclaims and the fence advances
    far = t0 + timedelta(minutes=10)
    lease2 = (await lease_store.acquire_runnable("worker-b", 5, far))[0]
    assert lease2.fence_token == lease1.fence_token + 1

    with pytest.raises(LeaseLostError):
        await event_store.append_new(
            iid,
            TENANT,
            [
                _status_event(iid),
            ],
            expected_version=1,
            guard=lease_store.make_guard(lease1),  # stale fence
        )


def _status_event(instance_id: str):
    from agentforge.core.events.types import InstanceStatusChanged

    return InstanceStatusChanged(
        event_id="x",
        instance_id=instance_id,
        tenant_id=TENANT,
        sequence=1,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        from_status="pending",
        to_status="running",
    )


async def test_end_to_end_worker_drives_to_completion(
    event_store, definitions, lease_store, sessionmaker
) -> None:
    iid = await _seed(event_store, definitions)

    reg = StepRegistry()

    async def fn(ctx: StepContext) -> StepResult:
        ctx.charge(0.01, model="m", tokens_input=5, tokens_output=2)
        return StepResult(output={"s": ctx.step_id}, model_used="m")

    reg.register("executor_agent", FunctionRunner(fn))

    driver = WorkflowDriver(
        event_store,
        definitions,
        StepExecutor(reg),
        DeadLetterService(sessionmaker),
        ids=SequentialIdGenerator("ev"),
    )
    worker = Worker(lease_store, driver, worker_id="w1", concurrency=4)

    results = await worker.run_once()
    assert results == [DriveResult.COMPLETED]

    inst = await event_store.get_instance(iid, TENANT, definition=linear_workflow(2))
    assert inst.status.value == "completed"
    assert inst.cost_accumulated_usd == pytest.approx(0.02)
    assert all(s.status.value == "completed" for s in inst.step_states.values())
