"""Narrow interfaces the executor / driver / worker depend on.

Concrete Postgres implementations live alongside this module; the test suite
supplies in-memory doubles. Structural (``Protocol``) typing means neither has to
inherit anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.events import BaseEvent
from agentforge.core.leasing import Guard, Lease


class EventJournal(Protocol):
    async def append_new(
        self,
        instance_id: str,
        tenant_id: str,
        drafts: Sequence[BaseEvent],
        *,
        expected_version: int,
        guard: Guard | None = None,
        next_wakeup_at: datetime | None = None,
    ) -> tuple[int, list[BaseEvent]]: ...

    async def get_instance(
        self,
        instance_id: str,
        tenant_id: str,
        *,
        definition: WorkflowDefinition | None = None,
    ) -> WorkflowInstance | None: ...

    async def load(
        self, instance_id: str, tenant_id: str, *, after: int = 0
    ) -> list[BaseEvent]: ...


class LeaseStore(Protocol):
    async def acquire_runnable(self, worker_id: str, limit: int, now: datetime) -> list[Lease]: ...

    async def heartbeat(
        self, worker_id: str, instance_ids: Sequence[str], now: datetime
    ) -> set[str]: ...

    async def release(self, worker_id: str, instance_id: str) -> None: ...

    async def reclaim_expired(self, now: datetime) -> list[str]: ...

    def make_guard(self, lease: Lease) -> Guard: ...


class DefinitionSource(Protocol):
    async def get(
        self, workflow_id: str, version: str, *, tenant_id: str
    ) -> WorkflowDefinition | None: ...

    async def get_active(self, name: str, *, tenant_id: str) -> WorkflowDefinition | None: ...
