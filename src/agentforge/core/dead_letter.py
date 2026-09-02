"""Dead-letter queue.

When a workflow exhausts retries (or hits a non-retryable failure with
``on_failure=dead_letter``) the driver records it here and moves the instance to
``DEAD_LETTERED``. Phase 3 adds requeue/bulk-resolve; for now this is the
record + list surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.persistence.tables import DeadLetterRow


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
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

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

    async def mark_resolved(self, dlq_id: int, *, tenant_id: str, now: datetime) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(DeadLetterRow)
                .where(
                    DeadLetterRow.id == dlq_id,
                    DeadLetterRow.tenant_id == tenant_id,
                )
                .values(resolved=True, resolved_at=now)
            )
