"""``InstanceService`` — create and read workflow instances.

Phase 1 scope: genesis (``InstanceCreated``) and projection reads. Step
scheduling / execution arrives in Phase 2 and appends further events through the
same :class:`EventStore`.
"""

from __future__ import annotations

from typing import Any

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import TriggerSource
from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.events.types import InstanceCreated
from agentforge.core.persistence.definition_repo import DefinitionRepository
from agentforge.core.persistence.event_store import EventStore
from agentforge.core.ports import (
    SYSTEM_CLOCK,
    UUID_GENERATOR,
    Clock,
    IdGenerator,
)
from agentforge.exceptions import ConfigurationError


class InstanceService:
    def __init__(
        self,
        event_store: EventStore,
        definitions: DefinitionRepository,
        *,
        clock: Clock = SYSTEM_CLOCK,
        ids: IdGenerator = UUID_GENERATOR,
    ) -> None:
        self._events = event_store
        self._defs = definitions
        self._clock = clock
        self._ids = ids

    async def _resolve_definition(
        self,
        *,
        tenant_id: str,
        workflow_id: str | None,
        version: str | None,
        name: str | None,
    ) -> WorkflowDefinition:
        if workflow_id and version:
            defn = await self._defs.get(workflow_id, version, tenant_id=tenant_id)
        elif name:
            defn = await self._defs.get_active(name, tenant_id=tenant_id)
        else:
            raise ConfigurationError("provide (workflow_id, version) or name")
        if defn is None:
            raise ConfigurationError("workflow definition not found")
        return defn

    async def create_instance(
        self,
        *,
        tenant_id: str,
        workflow_id: str | None = None,
        version: str | None = None,
        name: str | None = None,
        context: dict[str, Any] | None = None,
        budget_limit_usd: float | None = None,
        trigger_source: TriggerSource = TriggerSource.API,
        trigger_metadata: dict[str, Any] | None = None,
    ) -> WorkflowInstance:
        defn = await self._resolve_definition(
            tenant_id=tenant_id, workflow_id=workflow_id, version=version, name=name
        )
        instance_id = self._ids.new_id()
        genesis = InstanceCreated(
            event_id=self._ids.new_id(),
            instance_id=instance_id,
            tenant_id=tenant_id,
            sequence=1,
            occurred_at=self._clock.now(),
            workflow_id=defn.workflow_id,
            workflow_version=defn.version,
            context=context or {},
            budget_limit_usd=budget_limit_usd,
            trigger_source=trigger_source.value,
            trigger_metadata=trigger_metadata or {},
        )
        await self._events.append(instance_id, tenant_id, [genesis], expected_version=0)
        result = await self._events.get_instance(instance_id, tenant_id, definition=defn)
        assert result is not None
        return result

    async def get_instance(self, instance_id: str, *, tenant_id: str) -> WorkflowInstance | None:
        head = await self._events.get_instance(instance_id, tenant_id)
        if head is None:
            return None
        defn = await self._defs.get(head.workflow_id, head.workflow_version, tenant_id=tenant_id)
        return await self._events.get_instance(instance_id, tenant_id, definition=defn)
