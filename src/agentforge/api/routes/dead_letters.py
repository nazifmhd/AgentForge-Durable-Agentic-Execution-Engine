from __future__ import annotations

from fastapi import APIRouter, Depends

from agentforge.api.deps import ApiDeps, get_deps, require
from agentforge.api.schemas import DeadLetterView, RequeueResponse
from agentforge.core.auth import Principal, Scope

router = APIRouter(prefix="/api/v1/dead-letters", tags=["dead-letters"])

_READ = require(Scope.DLQ_READ, Scope.DLQ_WRITE)
_WRITE = require(Scope.DLQ_WRITE)


@router.get("")
async def list_dead_letters(
    resolved: bool = False,
    limit: int = 100,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_READ),
) -> list[DeadLetterView]:
    rows = await deps.dead_letters.list(
        tenant_id=principal.tenant_id, resolved=resolved, limit=limit
    )
    return [
        DeadLetterView(
            id=r.id,
            instance_id=r.instance_id,
            step_id=r.step_id,
            reason=r.reason,
            error_type=r.error_type,
            error_message=r.error_message,
            at_version=r.at_version,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/{dlq_id}/requeue")
async def requeue(
    dlq_id: int,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_WRITE),
) -> RequeueResponse:
    instance_id = await deps.dead_letters.requeue(
        dlq_id, tenant_id=principal.tenant_id, journal=deps.events
    )
    inst = await deps.instances.get_instance(instance_id, tenant_id=principal.tenant_id)
    return RequeueResponse(
        instance_id=instance_id,
        status=inst.status.value if inst else "unknown",
    )
