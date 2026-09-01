from __future__ import annotations

import asyncio

import pytest
from tests.factories import T0, StreamBuilder, linear_workflow

from agentforge.core.domain.enums import StepStatus, TriggerSource, WorkflowStatus
from agentforge.core.events.types import CostCharged
from agentforge.core.instances import InstanceService
from agentforge.core.persistence.definition_repo import DefinitionRepository
from agentforge.core.persistence.event_store import EventStore
from agentforge.core.persistence.tables import InstanceIndexRow, InstanceSnapshotRow
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.exceptions import ConfigurationError, ConflictError

pytestmark = pytest.mark.integration
TENANT = "tenant-1"


async def _service(event_store: EventStore, definitions: DefinitionRepository) -> InstanceService:
    await definitions.register(linear_workflow(3), tenant_id=TENANT)
    return InstanceService(
        event_store,
        definitions,
        clock=FixedClock(T0),
        ids=SequentialIdGenerator("x"),
    )


def _resume(instance_id: str) -> StreamBuilder:
    """Builder that continues after a persisted genesis event (sequence 1)."""
    return StreamBuilder(instance_id=instance_id, tenant_id=TENANT, start_sequence=1)


async def test_create_and_read_instance(event_store, definitions) -> None:
    svc = await _service(event_store, definitions)
    inst = await svc.create_instance(
        tenant_id=TENANT,
        workflow_id="wf-linear",
        version="1.0.0",
        context={"lead": "acme"},
        budget_limit_usd=0.5,
        trigger_source=TriggerSource.API,
    )
    assert inst.status is WorkflowStatus.PENDING
    assert inst.version == 1
    assert set(inst.step_states) == {"step_1", "step_2", "step_3"}

    again = await svc.get_instance(inst.instance_id, tenant_id=TENANT)
    assert again is not None
    assert again.context == {"lead": "acme"}


async def test_missing_definition_rejected(event_store, definitions) -> None:
    svc = InstanceService(event_store, definitions)
    with pytest.raises(ConfigurationError):
        await svc.create_instance(tenant_id=TENANT, workflow_id="ghost", version="9.9.9")


async def test_append_advances_version_and_index(event_store, definitions) -> None:
    svc = await _service(event_store, definitions)
    inst = await svc.create_instance(tenant_id=TENANT, workflow_id="wf-linear", version="1.0.0")
    b = _resume(inst.instance_id)
    b.wf_status("pending", "running")
    b.step_status("step_1", "pending", "ready")
    b.step_started("step_1")
    b.cost(0.07, step_id="step_1", tokens_input=10, tokens_output=4)
    b.step_completed("step_1", output={"ok": True})

    new_version = await event_store.append(inst.instance_id, TENANT, b.events, expected_version=1)
    assert new_version == 6

    projected = await event_store.get_instance(
        inst.instance_id, TENANT, definition=linear_workflow(3)
    )
    assert projected is not None
    assert projected.status is WorkflowStatus.RUNNING
    assert projected.step("step_1").status is StepStatus.COMPLETED
    assert projected.cost_accumulated_usd == pytest.approx(0.07)

    async with event_store._sm() as s:
        idx = await s.get(InstanceIndexRow, inst.instance_id)
        assert idx is not None
        assert idx.status == "running"
        assert idx.last_sequence == 6
        assert idx.cost_accumulated_usd == pytest.approx(0.07)


async def test_optimistic_concurrency_conflict(event_store, definitions) -> None:
    svc = await _service(event_store, definitions)
    inst = await svc.create_instance(tenant_id=TENANT, workflow_id="wf-linear", version="1.0.0")

    def batch() -> list:
        b = _resume(inst.instance_id)
        b.wf_status("pending", "running")
        return b.events

    await event_store.append(inst.instance_id, TENANT, batch(), expected_version=1)
    with pytest.raises(ConflictError):
        await event_store.append(inst.instance_id, TENANT, batch(), expected_version=1)


async def test_concurrent_appends_only_one_wins(event_store, definitions) -> None:
    svc = await _service(event_store, definitions)
    inst = await svc.create_instance(tenant_id=TENANT, workflow_id="wf-linear", version="1.0.0")

    def batch() -> list:
        b = _resume(inst.instance_id)
        b.wf_status("pending", "running")
        return b.events

    results = await asyncio.gather(
        event_store.append(inst.instance_id, TENANT, batch(), expected_version=1),
        event_store.append(inst.instance_id, TENANT, batch(), expected_version=1),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert len(ok) == 1
    assert len(conflicts) == 1


async def test_snapshot_written_and_used(event_store, definitions) -> None:
    svc = await _service(event_store, definitions)
    inst = await svc.create_instance(tenant_id=TENANT, workflow_id="wf-linear", version="1.0.0")
    b = _resume(inst.instance_id)
    b.wf_status("pending", "running")
    for _ in range(60):
        b.raw(CostCharged, amount_usd=0.001)
    await event_store.append(inst.instance_id, TENANT, b.events, expected_version=1)

    async with event_store._sm() as s:
        snap = await s.get(InstanceSnapshotRow, inst.instance_id)
        assert snap is not None
        assert snap.version >= 50

    projected = await event_store.get_instance(inst.instance_id, TENANT)
    assert projected is not None
    assert projected.cost_accumulated_usd == pytest.approx(0.06, abs=1e-6)
    assert projected.version == b.events[-1].sequence
