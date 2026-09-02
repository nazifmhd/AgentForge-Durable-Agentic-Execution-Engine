from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from tests.doubles import RecordingActionProvider
from tests.factories import make_step

from agentforge.core.dead_letter import DeadLetterService
from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import OnFailure
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.executor import StepExecutor
from agentforge.core.instances import InstanceService
from agentforge.core.leasing import PgLeaseStore
from agentforge.core.outbox import OutboxEntry, PgOutboxStore
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.core.side_effects import EffectStatus, SideEffectGuard
from agentforge.integrations.actions.base import ProviderRegistry
from agentforge.worker import Worker

pytestmark = pytest.mark.integration
TENANT = "tenant-1"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def test_outbox_claim_is_atomic_under_concurrency(sessionmaker) -> None:
    store = PgOutboxStore(sessionmaker)

    def entry() -> OutboxEntry:
        return OutboxEntry(
            idempotency_key="k1",
            instance_id="i1",
            tenant_id=TENANT,
            step_id="s1",
            effect_name="e",
            provider="test",
            params={"a": 1},
        )

    results = await asyncio.gather(*(store.claim(entry()) for _ in range(5)))
    # exactly one row, attempts advanced once per claim after the first insert
    assert max(r.attempts for r in results) == 5
    rows = await store.list_for_instance("i1", TENANT)
    assert len(rows) == 1


async def test_guard_dedups_against_real_outbox(sessionmaker) -> None:
    provider = RecordingActionProvider(idempotent={"send"})
    reg = ProviderRegistry()
    reg.register(provider)
    guard = SideEffectGuard(PgOutboxStore(sessionmaker), reg, clock=FixedClock(T0))

    for _ in range(3):
        await guard.execute(
            instance_id="i1",
            tenant_id=TENANT,
            step_id="s1",
            effect_name="send",
            params={"to": "x"},
            provider_name="test",
        )
    assert len(provider.executed) == 1
    rows = await guard.list_effects("i1", TENANT)
    assert [r.status for r in rows] == [EffectStatus.EXECUTED]


async def test_dead_letter_requeue_round_trip(event_store, definitions, sessionmaker) -> None:
    calls = {"n": 0}

    async def once_bad(ctx: StepContext) -> StepResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("permanent the first time")
        return StepResult(output={})

    reg = StepRegistry()
    reg.register("executor_agent", FunctionRunner(once_bad))

    wf = WorkflowDefinition(
        workflow_id="wf-dlq",
        name="dlq",
        version="1.0.0",
        on_failure=OnFailure.DEAD_LETTER,
        steps=(make_step("a"),),
    )
    await definitions.register(wf, tenant_id=TENANT)
    svc = InstanceService(
        event_store, definitions, clock=FixedClock(T0), ids=SequentialIdGenerator("i")
    )
    inst = await svc.create_instance(tenant_id=TENANT, workflow_id="wf-dlq", version="1.0.0")

    dlq = DeadLetterService(sessionmaker, clock=FixedClock(T0), ids=SequentialIdGenerator("e"))
    leases = PgLeaseStore(sessionmaker)
    driver = WorkflowDriver(
        event_store, definitions, StepExecutor(reg), dlq, ids=SequentialIdGenerator("ev")
    )
    worker = Worker(leases, driver, worker_id="w1", concurrency=2)

    assert await worker.run_once() == [DriveResult.DEAD_LETTERED]
    entries = await dlq.list(tenant_id=TENANT)
    assert len(entries) == 1

    requeued_id = await dlq.requeue(entries[0].id, tenant_id=TENANT, journal=event_store)
    assert requeued_id == inst.instance_id

    assert await worker.run_once() == [DriveResult.COMPLETED]
    final = await event_store.get_instance(inst.instance_id, TENANT, definition=wf)
    assert final.status.value == "completed"
    assert calls["n"] == 2
    assert (await dlq.list(tenant_id=TENANT)) == []
