"""Instance leasing — how workers claim exclusive execution rights (ADR-0004).

A lease row per instance carries the owning ``worker_id``, an ``expires_at`` the
owner extends by heartbeat, and a monotonic ``fence_token`` bumped on every
(re)acquisition. Every event append made while driving an instance runs a guard
that re-checks the fence against the lease row *inside the same transaction*, so
a worker that lost its lease (crash, partition, slow heartbeat) cannot write.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge.core.persistence.tables import InstanceIndexRow, InstanceLeaseRow
from agentforge.exceptions import LeaseLostError

# The argument is the append transaction's session (an AsyncSession for the
# Postgres store, ``None`` for the in-memory test double) — opaque to callers.
Guard = Callable[[Any], Awaitable[None]]

_RUNNABLE_STATUSES = ("pending", "running", "retrying")


@dataclass(frozen=True, slots=True)
class Lease:
    instance_id: str
    tenant_id: str
    workflow_id: str
    workflow_version: str
    worker_id: str
    fence_token: int
    expires_at: datetime
    last_sequence: int


class PgLeaseStore:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        lease_seconds: int = 30,
    ) -> None:
        self._sm = sessionmaker
        self._ttl = timedelta(seconds=lease_seconds)

    async def acquire_runnable(self, worker_id: str, limit: int, now: datetime) -> list[Lease]:
        if limit <= 0:
            return []
        i, lease = InstanceIndexRow, InstanceLeaseRow
        claim = (
            select(
                i.instance_id,
                i.tenant_id,
                i.workflow_id,
                i.workflow_version,
                i.last_sequence,
            )
            .outerjoin(lease, lease.instance_id == i.instance_id)
            .where(
                i.status.in_(_RUNNABLE_STATUSES),
                or_(i.next_wakeup_at.is_(None), i.next_wakeup_at <= now),
                or_(lease.instance_id.is_(None), lease.expires_at < now),
            )
            .order_by(i.updated_at)
            .limit(limit)
            .with_for_update(skip_locked=True, of=i)
        )

        leases: list[Lease] = []
        async with self._sm() as session, session.begin():
            rows = (await session.execute(claim)).all()
            for row in rows:
                expires_at = now + self._ttl
                insert_stmt = pg_insert(InstanceLeaseRow).values(
                    instance_id=row.instance_id,
                    tenant_id=row.tenant_id,
                    worker_id=worker_id,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=expires_at,
                    fence_token=1,
                )
                upsert = insert_stmt.on_conflict_do_update(
                    index_elements=[InstanceLeaseRow.instance_id],
                    set_={
                        "worker_id": worker_id,
                        "acquired_at": now,
                        "heartbeat_at": now,
                        "expires_at": expires_at,
                        "fence_token": InstanceLeaseRow.fence_token + 1,
                    },
                ).returning(InstanceLeaseRow.fence_token)
                fence = await session.scalar(upsert)
                assert fence is not None  # RETURNING on an upsert always yields a row
                leases.append(
                    Lease(
                        instance_id=row.instance_id,
                        tenant_id=row.tenant_id,
                        workflow_id=row.workflow_id,
                        workflow_version=row.workflow_version,
                        worker_id=worker_id,
                        fence_token=int(fence),
                        expires_at=expires_at,
                        last_sequence=row.last_sequence,
                    )
                )
        return leases

    async def heartbeat(
        self, worker_id: str, instance_ids: Sequence[str], now: datetime
    ) -> set[str]:
        if not instance_ids:
            return set()
        stmt = (
            update(InstanceLeaseRow)
            .where(
                InstanceLeaseRow.worker_id == worker_id,
                InstanceLeaseRow.instance_id.in_(list(instance_ids)),
            )
            .values(heartbeat_at=now, expires_at=now + self._ttl)
            .returning(InstanceLeaseRow.instance_id)
        )
        async with self._sm() as session, session.begin():
            rows = await session.execute(stmt)
            return {r.instance_id for r in rows}

    async def release(self, worker_id: str, instance_id: str) -> None:
        stmt = (
            update(InstanceLeaseRow)
            .where(
                InstanceLeaseRow.worker_id == worker_id,
                InstanceLeaseRow.instance_id == instance_id,
            )
            .values(expires_at=InstanceLeaseRow.heartbeat_at)
        )
        async with self._sm() as session, session.begin():
            await session.execute(stmt)

    async def reclaim_expired(self, now: datetime) -> list[str]:
        """Report instances whose lease has lapsed. State is not mutated —
        ``acquire_runnable`` already treats an expired lease as claimable; this
        exists for observability and the recovery sweep's logging."""
        stmt = select(InstanceLeaseRow.instance_id).where(InstanceLeaseRow.expires_at < now)
        async with self._sm() as session:
            rows = await session.execute(stmt)
            return [r.instance_id for r in rows]

    def make_guard(self, lease: Lease) -> Guard:
        stmt = select(InstanceLeaseRow.worker_id, InstanceLeaseRow.fence_token).where(
            InstanceLeaseRow.instance_id == lease.instance_id
        )

        async def _guard(session: AsyncSession) -> None:
            row = (await session.execute(stmt)).first()
            if (
                row is None
                or row.worker_id != lease.worker_id
                or row.fence_token != lease.fence_token
            ):
                raise LeaseLostError(f"worker {lease.worker_id} no longer owns {lease.instance_id}")

        return _guard
