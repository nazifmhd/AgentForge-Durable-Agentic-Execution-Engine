"""In-memory implementations of the journal / lease-store / definition-source
protocols, so the executor, driver, and worker can be tested without Postgres.

They mirror the Postgres semantics that matter: optimistic-concurrency conflict
on the version, a fencing guard that rejects a lost lease, and an index that
gates claiming by status / wakeup time / lease liveness.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.events import BaseEvent, fold
from agentforge.core.events.types import InstanceCreated
from agentforge.core.leasing import Guard, Lease
from agentforge.exceptions import ConflictError, LeaseLostError

_RUNNABLE = {"pending", "running", "retrying"}


@dataclass(slots=True)
class _IndexEntry:
    instance_id: str
    tenant_id: str
    workflow_id: str
    workflow_version: str
    status: str
    last_sequence: int
    next_wakeup_at: datetime | None
    updated_at: datetime


class InMemoryJournal:
    def __init__(self) -> None:
        self._events: dict[str, list[BaseEvent]] = {}
        self.index: dict[str, _IndexEntry] = {}
        self.append_calls = 0
        self.now: datetime = datetime(2026, 1, 1, tzinfo=UTC)

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
        if guard is not None:
            await guard(None)
        stream = self._events.setdefault(instance_id, [])
        current = stream[-1].sequence if stream else 0
        if current != expected_version:
            raise ConflictError(
                f"instance {instance_id}: expected v{expected_version}, found v{current}"
            )
        sequenced = [
            d.model_copy(update={"sequence": current + i}) for i, d in enumerate(drafts, start=1)
        ]
        stream.extend(sequenced)
        self.append_calls += 1
        inst = fold(stream)
        self.index[instance_id] = _IndexEntry(
            instance_id=instance_id,
            tenant_id=tenant_id,
            workflow_id=inst.workflow_id,
            workflow_version=inst.workflow_version,
            status=inst.status.value,
            last_sequence=inst.version,
            next_wakeup_at=next_wakeup_at,
            updated_at=sequenced[-1].occurred_at,
        )
        return sequenced[-1].sequence, sequenced

    async def get_instance(
        self,
        instance_id: str,
        tenant_id: str,
        *,
        definition: WorkflowDefinition | None = None,
    ) -> WorkflowInstance | None:
        stream = self._events.get(instance_id)
        if not stream:
            return None
        inst = fold(stream, definition=definition)
        return inst if inst.tenant_id == tenant_id else None

    async def load(self, instance_id: str, tenant_id: str, *, after: int = 0) -> list[BaseEvent]:
        return [e for e in self._events.get(instance_id, []) if e.sequence > after]


@dataclass(slots=True)
class _LeaseRow:
    worker_id: str
    expires_at: datetime
    fence_token: int
    heartbeat_at: datetime


class InMemoryLeaseStore:
    def __init__(self, journal: InMemoryJournal, *, lease_seconds: int = 30) -> None:
        self._journal = journal
        self._ttl = timedelta(seconds=lease_seconds)
        self._leases: dict[str, _LeaseRow] = {}

    async def acquire_runnable(self, worker_id: str, limit: int, now: datetime) -> list[Lease]:
        out: list[Lease] = []
        entries = sorted(self._journal.index.values(), key=lambda e: e.updated_at)
        for entry in entries:
            if len(out) >= limit:
                break
            if entry.status not in _RUNNABLE:
                continue
            if entry.next_wakeup_at is not None and entry.next_wakeup_at > now:
                continue
            row = self._leases.get(entry.instance_id)
            if row is not None and row.expires_at >= now:
                continue
            fence = (row.fence_token + 1) if row is not None else 1
            expires = now + self._ttl
            self._leases[entry.instance_id] = _LeaseRow(worker_id, expires, fence, now)
            out.append(
                Lease(
                    instance_id=entry.instance_id,
                    tenant_id=entry.tenant_id,
                    workflow_id=entry.workflow_id,
                    workflow_version=entry.workflow_version,
                    worker_id=worker_id,
                    fence_token=fence,
                    expires_at=expires,
                    last_sequence=entry.last_sequence,
                )
            )
        return out

    async def heartbeat(
        self, worker_id: str, instance_ids: Sequence[str], now: datetime
    ) -> set[str]:
        alive: set[str] = set()
        for iid in instance_ids:
            row = self._leases.get(iid)
            if row is not None and row.worker_id == worker_id:
                row.expires_at = now + self._ttl
                row.heartbeat_at = now
                alive.add(iid)
        return alive

    async def release(self, worker_id: str, instance_id: str) -> None:
        row = self._leases.get(instance_id)
        if row is not None and row.worker_id == worker_id:
            row.expires_at = row.heartbeat_at

    async def reclaim_expired(self, now: datetime) -> list[str]:
        return [iid for iid, row in self._leases.items() if row.expires_at < now]

    def make_guard(self, lease: Lease) -> Guard:
        async def _guard(_session: object) -> None:
            row = self._leases.get(lease.instance_id)
            if (
                row is None
                or row.worker_id != lease.worker_id
                or row.fence_token != lease.fence_token
            ):
                raise LeaseLostError(f"worker {lease.worker_id} lost lease on {lease.instance_id}")

        return _guard

    def expire_all(self) -> None:
        """Simulate every current owner dying."""
        for row in self._leases.values():
            row.expires_at = row.heartbeat_at - timedelta(seconds=1)


class InMemoryDefinitions:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], WorkflowDefinition] = {}

    def add(self, definition: WorkflowDefinition, *, tenant_id: str = "tenant-1") -> None:
        self._by_key[(tenant_id, definition.workflow_id, definition.version)] = definition

    async def get(
        self, workflow_id: str, version: str, *, tenant_id: str
    ) -> WorkflowDefinition | None:
        return self._by_key.get((tenant_id, workflow_id, version))


class FakeDeadLetters:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def record(self, instance: WorkflowInstance, *, step_id: str | None, reason: str) -> None:
        self.records.append(
            {
                "instance_id": instance.instance_id,
                "step_id": step_id,
                "reason": reason,
                "version": instance.version,
            }
        )


@dataclass(slots=True)
class Harness:
    journal: InMemoryJournal
    leases: InMemoryLeaseStore
    definitions: InMemoryDefinitions
    dead_letters: FakeDeadLetters = field(default_factory=FakeDeadLetters)


async def seed_instance(
    journal: InMemoryJournal,
    definition: WorkflowDefinition,
    *,
    instance_id: str = "inst-1",
    tenant_id: str = "tenant-1",
    context: dict | None = None,
    budget_limit_usd: float | None = None,
    occurred_at: datetime = datetime(2026, 1, 1, tzinfo=UTC),
) -> str:
    genesis = InstanceCreated(
        event_id="genesis",
        instance_id=instance_id,
        tenant_id=tenant_id,
        sequence=1,
        occurred_at=occurred_at,
        workflow_id=definition.workflow_id,
        workflow_version=definition.version,
        context=context or {},
        budget_limit_usd=budget_limit_usd,
    )
    await journal.append_new(instance_id, tenant_id, [genesis], expected_version=0)
    return instance_id
