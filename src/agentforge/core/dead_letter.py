"""Dead-letter queue.

When a workflow exhausts retries (or hits a non-retryable failure with
``on_failure=dead_letter``) the driver records it here and moves the instance to
``DEAD_LETTERED``. An operator can ``requeue`` an entry: that appends a
``WorkflowRequeued`` event (instance ``DEAD_LETTERED -> RUNNING``, the failed
step reset to ``READY`` with a fresh retry budget) so a worker picks it up again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.events.types import WorkflowRequeued
from agentforge.core.persistence.protocols import EventJournal
from agentforge.core.persistence.tables import DeadLetterRow
from agentforge.core.ports import SYSTEM_CLOCK, UUID_GENERATOR, Clock, IdGenerator
from agentforge.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class DeadLetter:
    id: int
    instance_id: str
    tenant_id: str
    step_id: str | None
    reason: str
    error_type: str | None
    error_message: str | None
    at_version: int
    resolved: bool
    created_at: datetime


class DeadLetterService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        clock: Clock = SYSTEM_CLOCK,
        ids: IdGenerator = UUID_GENERATOR,
    ) -> None:
        self._sm = sessionmaker
        self._clock = clock
        self._ids = ids

    async def record(
        self,
        instance: WorkflowInstance,
        *,
        step_id: str | None,
        reason: str,
    ) -> None:
        last_err = instance.error_history[-1] if instance.error_history else None
        async with self._sm() as session, session.begin():
            session.add(
                DeadLetterRow(
                    instance_id=instance.instance_id,
                    tenant_id=instance.tenant_id,
                    step_id=step_id,
                    reason=reason,
                    error_type=last_err.error_type if last_err else None,
                    error_message=last_err.error_message if last_err else None,
                    at_version=instance.version,
                )
            )

    async def list(
        self, *, tenant_id: str, resolved: bool = False, limit: int = 100
    ) -> list[DeadLetter]:
        async with self._sm() as session:
            rows = await session.scalars(
                select(DeadLetterRow)
                .where(
                    DeadLetterRow.tenant_id == tenant_id,
                    DeadLetterRow.resolved.is_(resolved),
                )
                .order_by(DeadLetterRow.created_at.desc())
                .limit(limit)
            )
            return [
                DeadLetter(
                    id=r.id,
                    instance_id=r.instance_id,
                    tenant_id=r.tenant_id,
                    step_id=r.step_id,
                    reason=r.reason,
                    error_type=r.error_type,
                    error_message=r.error_message,
                    at_version=r.at_version,
                    resolved=r.resolved,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def mark_resolved(
        self, dlq_id: int, *, tenant_id: str, now: datetime | None = None
    ) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(DeadLetterRow)
                .where(
                    DeadLetterRow.id == dlq_id,
                    DeadLetterRow.tenant_id == tenant_id,
                )
                .values(resolved=True, resolved_at=now or self._clock.now())
            )

    async def requeue(
        self,
        dlq_id: int,
        *,
        tenant_id: str,
        journal: EventJournal,
        requeued_by: str = "operator",
    ) -> str:
        """Move a dead-lettered instance back to RUNNING. Returns its instance_id."""
        async with self._sm() as session:
            row = await session.get(DeadLetterRow, dlq_id)
        if row is None or row.tenant_id != tenant_id:
            raise ConfigurationError(f"dead-letter entry {dlq_id} not found")
        if row.resolved:
            raise ConfigurationError(f"dead-letter entry {dlq_id} already resolved")

        instance = await journal.get_instance(row.instance_id, tenant_id)
        if instance is None:
            raise ConfigurationError(f"instance {row.instance_id} not found")
        if instance.status.value != "dead_lettered":
            raise ConfigurationError(
                f"instance {row.instance_id} is {instance.status.value}, not dead_lettered"
            )

        event = WorkflowRequeued(
            event_id=self._ids.new_id(),
            instance_id=row.instance_id,
            tenant_id=tenant_id,
            sequence=1,  # placeholder; append_new assigns the real sequence
            occurred_at=self._clock.now(),
            step_id=row.step_id,
            requeued_by=requeued_by,
            dlq_id=dlq_id,
        )
        await journal.append_new(
            row.instance_id, tenant_id, [event], expected_version=instance.version
        )
        await self.mark_resolved(dlq_id, tenant_id=tenant_id)
        return row.instance_id
