"""Operator instance control: pause / resume / abort.

These append a status-change event with no lease guard — an operator has no
lease. If a worker is mid-drive, the API's append bumps the version, the worker's
next append hits a ``ConflictError`` and it bails (``LEASE_LOST``), and the
operator's transition stands. A short optimistic retry rides out the race.
"""

from __future__ import annotations

from agentforge.core.domain.enums import (
    TERMINAL_WORKFLOW_STATUSES,
    WorkflowStatus,
    workflow_transition_allowed,
)
from agentforge.core.events import types as E
from agentforge.core.persistence.protocols import EventJournal
from agentforge.core.ports import SYSTEM_CLOCK, UUID_GENERATOR, Clock, IdGenerator
from agentforge.exceptions import ConfigurationError, ConflictError

_RETRIES = 5


class InstanceControl:
    def __init__(
        self,
        journal: EventJournal,
        *,
        clock: Clock = SYSTEM_CLOCK,
        ids: IdGenerator = UUID_GENERATOR,
    ) -> None:
        self._journal = journal
        self._clock = clock
        self._ids = ids

    async def pause(self, instance_id: str, tenant_id: str, *, by: str) -> None:
        await self._transition(instance_id, tenant_id, WorkflowStatus.PAUSED, f"paused by {by}")

    async def resume(self, instance_id: str, tenant_id: str, *, by: str) -> None:
        await self._transition(instance_id, tenant_id, WorkflowStatus.RUNNING, f"resumed by {by}")

    async def abort(self, instance_id: str, tenant_id: str, *, by: str) -> None:
        await self._transition(instance_id, tenant_id, WorkflowStatus.FAILED, f"aborted by {by}")

    async def _transition(
        self, instance_id: str, tenant_id: str, to: WorkflowStatus, reason: str
    ) -> None:
        for _ in range(_RETRIES):
            instance = await self._journal.get_instance(instance_id, tenant_id)
            if instance is None:
                raise ConfigurationError(f"instance {instance_id} not found")
            if instance.status in TERMINAL_WORKFLOW_STATUSES:
                raise ConfigurationError(
                    f"instance {instance_id} is {instance.status.value} (terminal)"
                )
            if instance.status == to:
                return
            if not workflow_transition_allowed(instance.status, to):
                raise ConfigurationError(f"cannot move {instance.status.value} -> {to.value}")
            event = E.InstanceStatusChanged(
                event_id=self._ids.new_id(),
                instance_id=instance_id,
                tenant_id=tenant_id,
                sequence=1,  # placeholder; append_new assigns
                occurred_at=self._clock.now(),
                from_status=instance.status,
                to_status=to,
                reason=reason,
            )
            try:
                await self._journal.append_new(
                    instance_id, tenant_id, [event], expected_version=instance.version
                )
                return
            except ConflictError:
                continue
        raise ConflictError(
            f"instance {instance_id}: could not apply {to.value} after {_RETRIES} tries"
        )
