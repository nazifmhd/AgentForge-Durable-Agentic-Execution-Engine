"""Outbox persistence for the side-effect guard.

Split behind a protocol (like the event journal and lease store) so the guard's
dedup / reconcile / compensate logic is unit-testable with an in-memory store,
while the Postgres store carries the real atomic-upsert-with-``RETURNING``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge.core.persistence.tables import SideEffectOutboxRow


@dataclass(slots=True)
class OutboxEntry:
    idempotency_key: str
    instance_id: str
    tenant_id: str
    step_id: str
    effect_name: str
    provider: str
    params: dict[str, Any]
    guarantee: str = "at_least_once_dedup"
    status: str = "pending"
    attempts: int = 0
    result: dict[str, Any] | None = None
    provider_ref: str | None = None
    error: str | None = None
    compensation_status: str | None = None


class OutboxStore(Protocol):
    async def claim(self, entry: OutboxEntry) -> OutboxEntry:
        """Atomic: insert ``entry`` (attempts=1) if absent, else lock the existing
        row and increment its attempts. Returns the current row either way."""
        ...

    async def mark(self, key: str, **fields: Any) -> None: ...

    async def list_for_instance(
        self, instance_id: str, tenant_id: str, *, status: str | None = None
    ) -> list[OutboxEntry]: ...


def _row_to_entry(r: SideEffectOutboxRow) -> OutboxEntry:
    return OutboxEntry(
        idempotency_key=r.idempotency_key,
        instance_id=r.instance_id,
        tenant_id=r.tenant_id,
        step_id=r.step_id,
        effect_name=r.effect_name,
        provider=r.provider,
        params=r.params,
        guarantee=r.guarantee,
        status=r.status,
        attempts=r.attempts,
        result=r.result,
        provider_ref=r.provider_ref,
        error=r.error,
        compensation_status=r.compensation_status,
    )


class PgOutboxStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def claim(self, entry: OutboxEntry) -> OutboxEntry:
        stmt = (
            pg_insert(SideEffectOutboxRow)
            .values(
                idempotency_key=entry.idempotency_key,
                instance_id=entry.instance_id,
                tenant_id=entry.tenant_id,
                step_id=entry.step_id,
                effect_name=entry.effect_name,
                provider=entry.provider,
                params=entry.params,
                guarantee=entry.guarantee,
                status="pending",
                attempts=1,
            )
            .on_conflict_do_update(
                index_elements=[SideEffectOutboxRow.idempotency_key],
                set_={"attempts": SideEffectOutboxRow.attempts + 1},
            )
            .returning(SideEffectOutboxRow)
        )
        async with self._sm() as session, session.begin():
            row = (await session.execute(stmt)).scalar_one()
            return _row_to_entry(row)

    async def mark(self, key: str, **fields: Any) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(SideEffectOutboxRow)
                .where(SideEffectOutboxRow.idempotency_key == key)
                .values(**fields)
            )

    async def list_for_instance(
        self, instance_id: str, tenant_id: str, *, status: str | None = None
    ) -> list[OutboxEntry]:
        stmt = select(SideEffectOutboxRow).where(
            SideEffectOutboxRow.instance_id == instance_id,
            SideEffectOutboxRow.tenant_id == tenant_id,
        )
        if status is not None:
            stmt = stmt.where(SideEffectOutboxRow.status == status)
        stmt = stmt.order_by(SideEffectOutboxRow.created_at)
        async with self._sm() as session:
            rows = (await session.execute(stmt)).scalars()
            return [_row_to_entry(r) for r in rows]


class InMemoryOutboxStore:
    def __init__(self) -> None:
        self._rows: dict[str, OutboxEntry] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()

    async def claim(self, entry: OutboxEntry) -> OutboxEntry:
        async with self._lock:
            existing = self._rows.get(entry.idempotency_key)
            if existing is not None:
                existing.attempts += 1
                return replace(existing)
            fresh = replace(entry, status="pending", attempts=1)
            self._rows[entry.idempotency_key] = fresh
            self._order.append(entry.idempotency_key)
            return replace(fresh)

    async def mark(self, key: str, **fields: Any) -> None:
        async with self._lock:
            row = self._rows.get(key)
            if row is None:
                return
            for k, v in fields.items():
                if k == "updated_at":
                    continue
                setattr(row, k, v)

    async def list_for_instance(
        self, instance_id: str, tenant_id: str, *, status: str | None = None
    ) -> list[OutboxEntry]:
        out = [
            replace(self._rows[k])
            for k in self._order
            if self._rows[k].instance_id == instance_id
            and self._rows[k].tenant_id == tenant_id
            and (status is None or self._rows[k].status == status)
        ]
        return out
