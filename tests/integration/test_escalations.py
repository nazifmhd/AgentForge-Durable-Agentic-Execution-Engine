from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.doubles import FakeDeadLetters
from tests.factories import make_step

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.escalation import EscalationController, PgEscalationReadStore
from agentforge.core.executor import StepExecutor
from agentforge.core.instances import InstanceService
from agentforge.core.leasing import PgLeaseStore
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.worker import Worker

pytestmark = pytest.mark.integration
TENANT = "tenant-1"
T0 = datetime(2026, 9, 5, tzinfo=UTC)


def _wf(**step_kw) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="wf-appr",
        name="appr",
        version="1.0.0",
        steps=(make_step("a"), make_step("b", ("a",), requires_approval=True, **step_kw)),
    )


def _registry() -> StepRegistry:
    reg = StepRegistry()

    async def fn(ctx: StepContext) -> StepResult:
        return StepResult(output={"did": ctx.step_id})

    reg.register("executor_agent", FunctionRunner(fn))
    return reg


async def _run_to_approval(event_store, definitions, sessionmaker, wf):
    await definitions.register(wf, tenant_id=TENANT)
    svc = InstanceService(
        event_store, definitions, clock=FixedClock(T0), ids=SequentialIdGenerator("i")
    )
    inst = await svc.create_instance(tenant_id=TENANT, workflow_id="wf-appr", version="1.0.0")
    leases = PgLeaseStore(sessionmaker)
    driver = WorkflowDriver(
        event_store,
        definitions,
        StepExecutor(_registry()),
        FakeDeadLetters(),  # type: ignore[arg-type]
        clock=FixedClock(T0),
        ids=SequentialIdGenerator("ev"),
    )
    worker = Worker(leases, driver, worker_id="w1", concurrency=2)
    assert await worker.run_once() == [DriveResult.WAITING_APPROVAL]
    return inst.instance_id, leases, driver, worker


async def test_escalation_projected_and_resolved(event_store, definitions, sessionmaker) -> None:
    iid, _leases, _driver, worker = await _run_to_approval(
        event_store, definitions, sessionmaker, _wf()
    )
    controller = EscalationController(
        PgEscalationReadStore(sessionmaker),
        event_store,
        clock=FixedClock(T0),
        ids=SequentialIdGenerator("re"),
    )

    pending = await controller.list_pending(tenant_id=TENANT)
    assert len(pending) == 1
    assert pending[0].instance_id == iid
    assert pending[0].step_id == "b"

    await controller.resolve(
        pending[0].escalation_id,
        tenant_id=TENANT,
        resolution="approve",
        resolved_by="alice",
    )
    assert await controller.list_pending(tenant_id=TENANT) == []
    assert await worker.run_once() == [DriveResult.COMPLETED]


async def test_deadline_sweep_auto_actions(event_store, definitions, sessionmaker) -> None:
    iid, _leases, _driver, worker = await _run_to_approval(
        event_store,
        definitions,
        sessionmaker,
        _wf(approval_timeout_seconds=30, approval_auto_action="skip"),
    )
    controller = EscalationController(
        PgEscalationReadStore(sessionmaker),
        event_store,
        clock=FixedClock(T0),
        ids=SequentialIdGenerator("re"),
    )

    assert await controller.expire_due(T0 + timedelta(seconds=5)) == []
    fired = await controller.expire_due(T0 + timedelta(seconds=60))
    assert len(fired) == 1

    assert await worker.run_once() == [DriveResult.COMPLETED]
    inst = await event_store.get_instance(iid, TENANT, definition=_wf())
    assert inst.step_states["b"].status.value == "skipped"
