"""``EventStore`` — append-only persistence for the event log, plus the derived
snapshot and index rows kept consistent inside the same transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.events import (
    BaseEvent,
    Snapshot,
    dump_event,
    fold,
    parse_event,
    should_snapshot,
)
from agentforge.core.events import types as ET
from agentforge.core.leasing import Guard
from agentforge.core.persistence.tables import (
    EscalationRow,
    InstanceIndexRow,
    InstanceSnapshotRow,
    WorkflowEventRow,
)
from agentforge.core.pubsub import EventPublisher, NoopPublisher
from agentforge.exceptions import ConflictError, EventStreamError


class EventStore:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._publisher = publisher or NoopPublisher()

    # --- write path ------------------------------------------------------
    async def append(
        self,
        instance_id: str,
        tenant_id: str,
        events: Sequence[BaseEvent],
        *,
        expected_version: int,
        guard: Guard | None = None,
        next_wakeup_at: datetime | None = None,
    ) -> int:
        """Append pre-sequenced ``events`` (their ``sequence`` must be exactly
        ``expected_version + 1 ...``). Returns the new version."""
        if not events:
            return expected_version
        for offset, ev in enumerate(events, start=1):
            if ev.sequence != expected_version + offset:
                raise EventStreamError(
                    f"event {offset} has sequence {ev.sequence}, "
                    f"expected {expected_version + offset}"
                )
        result = await self._commit(
            instance_id,
            tenant_id,
            list(events),
            expected_version=expected_version,
            guard=guard,
            next_wakeup_at=next_wakeup_at,
        )
        return result[0]

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
        """Assign sequence numbers to ``drafts`` (from ``expected_version + 1``)
        and append them atomically. Returns ``(new_version, sequenced_events)``."""
        sequenced = [
            draft.model_copy(update={"sequence": expected_version + i})
            for i, draft in enumerate(drafts, start=1)
        ]
        return await self._commit(
            instance_id,
            tenant_id,
            sequenced,
            expected_version=expected_version,
            guard=guard,
            next_wakeup_at=next_wakeup_at,
        )

    async def _commit(
        self,
        instance_id: str,
        tenant_id: str,
        events: list[BaseEvent],
        *,
        expected_version: int,
        guard: Guard | None,
        next_wakeup_at: datetime | None,
    ) -> tuple[int, list[BaseEvent]]:
        for ev in events:
            if ev.instance_id != instance_id or ev.tenant_id != tenant_id:
                raise EventStreamError("event instance_id/tenant_id mismatch")

        async with self._sm() as session, session.begin():
            if guard is not None:
                await guard(session)

            current = await session.scalar(
                select(func.max(WorkflowEventRow.sequence)).where(
                    WorkflowEventRow.instance_id == instance_id
                )
            )
            current = current or 0
            if current != expected_version:
                raise ConflictError(
                    f"instance {instance_id}: expected v{expected_version}, found v{current}"
                )

            session.add_all(
                WorkflowEventRow(
                    instance_id=instance_id,
                    tenant_id=tenant_id,
                    sequence=ev.sequence,
                    event_type=ev.event_type,
                    payload=dump_event(ev),
                    occurred_at=ev.occurred_at,
                )
                for ev in events
            )
            try:
                await session.flush()
            except IntegrityError as exc:  # UNIQUE(instance_id, sequence) lost the race
                raise ConflictError(
                    f"instance {instance_id}: concurrent append at v{expected_version}"
                ) from exc

            instance = await self._project(session, instance_id, tenant_id)
            await self._upsert_index(session, instance, next_wakeup_at=next_wakeup_at)
            await self._project_escalations(session, events)

            snap = await self._load_snapshot(session, instance_id)
            base_version = snap.version if snap else 0
            if snap is None or should_snapshot(base_version, instance.version):
                await self._write_snapshot(session, instance, events[-1].occurred_at)

        await self._publisher.publish(instance_id, tenant_id, events)
        return events[-1].sequence, events

    async def _project_escalations(self, session: AsyncSession, events: list[BaseEvent]) -> None:
        for ev in events:
            if isinstance(ev, ET.EscalationRaised):
                stmt = pg_insert(EscalationRow).values(
                    escalation_id=ev.escalation_id,
                    instance_id=ev.instance_id,
                    tenant_id=ev.tenant_id,
                    step_id=ev.step_id,
                    reason=ev.reason,
                    recommendation=ev.recommendation,
                    confidence=ev.confidence,
                    options=ev.options,
                    auto_action=ev.auto_action,
                    deadline=ev.deadline,
                    status="pending",
                    created_at=ev.occurred_at,
                )
                await session.execute(
                    stmt.on_conflict_do_nothing(index_elements=[EscalationRow.escalation_id])
                )
            elif isinstance(ev, ET.EscalationResolved):
                await session.execute(
                    update(EscalationRow)
                    .where(EscalationRow.escalation_id == ev.escalation_id)
                    .values(
                        status="resolved",
                        resolution=ev.resolution,
                        resolved_by=ev.resolved_by,
                        resolved_at=ev.occurred_at,
                    )
                )
            elif isinstance(ev, ET.EscalationTimedOut):
                await session.execute(
                    update(EscalationRow)
                    .where(EscalationRow.escalation_id == ev.escalation_id)
                    .values(
                        status="timed_out",
                        resolution=ev.auto_action,
                        resolved_by="auto",
                        resolved_at=ev.occurred_at,
                    )
                )

    # --- read path -----------------------------------------------------
    async def load(self, instance_id: str, tenant_id: str, *, after: int = 0) -> list[BaseEvent]:
        async with self._sm() as session:
            rows = await session.scalars(
                select(WorkflowEventRow)
                .where(
                    WorkflowEventRow.instance_id == instance_id,
                    WorkflowEventRow.tenant_id == tenant_id,
                    WorkflowEventRow.sequence > after,
                )
                .order_by(WorkflowEventRow.sequence)
            )
            return [parse_event(r.payload) for r in rows]

    async def get_instance(
        self,
        instance_id: str,
        tenant_id: str,
        *,
        definition: WorkflowDefinition | None = None,
    ) -> WorkflowInstance | None:
        async with self._sm() as session:
            return await self._project(
                session, instance_id, tenant_id, definition=definition, required=False
            )

    async def state_at(
        self,
        instance_id: str,
        tenant_id: str,
        version: int,
        *,
        definition: WorkflowDefinition | None = None,
    ) -> WorkflowInstance | None:
        """Read-only time travel: the folded instance state as of ``version``."""
        events = [e for e in await self.load(instance_id, tenant_id) if e.sequence <= version]
        if not events:
            return None
        return fold(events, definition=definition)

    async def latest_snapshot(self, instance_id: str, tenant_id: str) -> Snapshot | None:
        async with self._sm() as session:
            snap = await self._load_snapshot(session, instance_id)
            if snap is None or snap.tenant_id != tenant_id:
                return None
            return snap

    # --- internals ----------------------------------------------------
    async def _project(
        self,
        session: AsyncSession,
        instance_id: str,
        tenant_id: str,
        *,
        definition: WorkflowDefinition | None = None,
        required: bool = True,
    ) -> WorkflowInstance:
        snap = await self._load_snapshot(session, instance_id)
        base = snap.state if snap else None
        after = snap.version if snap else 0
        rows = await session.scalars(
            select(WorkflowEventRow)
            .where(
                WorkflowEventRow.instance_id == instance_id,
                WorkflowEventRow.sequence > after,
            )
            .order_by(WorkflowEventRow.sequence)
        )
        tail = [parse_event(r.payload) for r in rows]
        if base is None and not tail:
            if required:
                raise EventStreamError(f"unknown instance {instance_id}")
            return None  # type: ignore[return-value]
        instance = fold(tail, base=base, definition=definition)
        if instance.tenant_id != tenant_id:
            if required:
                raise EventStreamError("tenant mismatch")
            return None  # type: ignore[return-value]
        return instance

    async def _load_snapshot(self, session: AsyncSession, instance_id: str) -> Snapshot | None:
        row = await session.get(InstanceSnapshotRow, instance_id)
        if row is None:
            return None
        return Snapshot(
            instance_id=row.instance_id,
            tenant_id=row.tenant_id,
            version=row.version,
            state=WorkflowInstance.model_validate(row.state),
            created_at=row.created_at,
        )

    async def _write_snapshot(
        self, session: AsyncSession, instance: WorkflowInstance, created_at: object
    ) -> None:
        values = {
            "instance_id": instance.instance_id,
            "tenant_id": instance.tenant_id,
            "version": instance.version,
            "state": instance.model_dump(mode="json"),
            "created_at": created_at,
        }
        stmt = pg_insert(InstanceSnapshotRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[InstanceSnapshotRow.instance_id],
            set_={
                "version": stmt.excluded.version,
                "state": stmt.excluded.state,
                "created_at": stmt.excluded.created_at,
            },
        )
        await session.execute(stmt)

    async def _upsert_index(
        self,
        session: AsyncSession,
        instance: WorkflowInstance,
        *,
        next_wakeup_at: datetime | None = None,
    ) -> None:
        values = {
            "instance_id": instance.instance_id,
            "tenant_id": instance.tenant_id,
            "workflow_id": instance.workflow_id,
            "workflow_version": instance.workflow_version,
            "status": instance.status.value,
            "last_sequence": instance.version,
            "cost_accumulated_usd": instance.cost_accumulated_usd,
            "budget_limit_usd": instance.budget_limit_usd,
            "next_wakeup_at": next_wakeup_at,
            "completed_at": instance.completed_at,
        }
        stmt = pg_insert(InstanceIndexRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[InstanceIndexRow.instance_id],
            set_={
                "status": stmt.excluded.status,
                "last_sequence": stmt.excluded.last_sequence,
                "cost_accumulated_usd": stmt.excluded.cost_accumulated_usd,
                "budget_limit_usd": stmt.excluded.budget_limit_usd,
                "next_wakeup_at": stmt.excluded.next_wakeup_at,
                "completed_at": stmt.excluded.completed_at,
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)
