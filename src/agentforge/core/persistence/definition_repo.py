"""Repository for workflow definitions (templates).

Every method is tenant-scoped (ADR-0010). Registering a definition is
idempotent on ``(tenant, workflow_id, version)`` *when the checksum matches*;
re-registering the same version with different content is rejected — bump the
version instead.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.persistence.tables import WorkflowDefinitionRow
from agentforge.exceptions import ConfigurationError


class DefinitionRepository:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def register(
        self, definition: WorkflowDefinition, *, tenant_id: str, activate: bool = True
    ) -> WorkflowDefinition:
        async with self._sm() as session, session.begin():
            existing = await session.get(
                WorkflowDefinitionRow,
                (tenant_id, definition.workflow_id, definition.version),
            )
            if existing is not None:
                if existing.checksum != definition.checksum:
                    raise ConfigurationError(
                        f"{definition.workflow_id} v{definition.version} already "
                        f"registered with different content — bump the version"
                    )
                return definition

            if activate:
                await session.execute(
                    update(WorkflowDefinitionRow)
                    .where(
                        WorkflowDefinitionRow.tenant_id == tenant_id,
                        WorkflowDefinitionRow.name == definition.name,
                    )
                    .values(is_active=False)
                )

            session.add(
                WorkflowDefinitionRow(
                    tenant_id=tenant_id,
                    workflow_id=definition.workflow_id,
                    version=definition.version,
                    name=definition.name,
                    description=definition.description,
                    definition=definition.model_dump(mode="json"),
                    checksum=definition.checksum,
                    is_active=activate,
                )
            )
        return definition

    async def get(
        self, workflow_id: str, version: str, *, tenant_id: str
    ) -> WorkflowDefinition | None:
        async with self._sm() as session:
            row = await session.get(WorkflowDefinitionRow, (tenant_id, workflow_id, version))
            return WorkflowDefinition.model_validate(row.definition) if row else None

    async def get_active(self, name: str, *, tenant_id: str) -> WorkflowDefinition | None:
        async with self._sm() as session:
            row = await session.scalar(
                select(WorkflowDefinitionRow).where(
                    WorkflowDefinitionRow.tenant_id == tenant_id,
                    WorkflowDefinitionRow.name == name,
                    WorkflowDefinitionRow.is_active.is_(True),
                )
            )
            return WorkflowDefinition.model_validate(row.definition) if row else None

    async def list(self, *, tenant_id: str, active_only: bool = False) -> list[WorkflowDefinition]:
        async with self._sm() as session:
            stmt = select(WorkflowDefinitionRow).where(WorkflowDefinitionRow.tenant_id == tenant_id)
            if active_only:
                stmt = stmt.where(WorkflowDefinitionRow.is_active.is_(True))
            stmt = stmt.order_by(WorkflowDefinitionRow.name, WorkflowDefinitionRow.version)
            rows = await session.scalars(stmt)
            return [WorkflowDefinition.model_validate(r.definition) for r in rows]
