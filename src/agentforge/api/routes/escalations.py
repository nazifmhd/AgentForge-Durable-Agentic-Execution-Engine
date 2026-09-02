from __future__ import annotations

from fastapi import APIRouter, Depends

from agentforge.api.deps import ApiDeps, get_deps, require
from agentforge.api.schemas import EscalationView, ResolveRequest
from agentforge.core.auth import Principal, Scope

router = APIRouter(prefix="/api/v1/escalations", tags=["escalations"])

_READ = require(Scope.ESCALATIONS_READ, Scope.ESCALATIONS_WRITE)
_WRITE = require(Scope.ESCALATIONS_WRITE)


@router.get("")
async def list_pending(
    limit: int = 100,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_READ),
) -> list[EscalationView]:
    pending = await deps.escalations.list_pending(tenant_id=principal.tenant_id, limit=limit)
    return [
        EscalationView(
            escalation_id=p.escalation_id,
            instance_id=p.instance_id,
            step_id=p.step_id,
            reason=p.reason,
            recommendation=p.recommendation,
            confidence=p.confidence,
            options=p.options,
            auto_action=p.auto_action,
            deadline=p.deadline,
            created_at=p.created_at,
        )
        for p in pending
    ]


@router.post("/{escalation_id}/resolve")
async def resolve(
    escalation_id: str,
    body: ResolveRequest,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_WRITE),
) -> dict[str, str]:
    instance_id = await deps.escalations.resolve(
        escalation_id,
        tenant_id=principal.tenant_id,
        resolution=body.resolution,
        resolved_by=body.resolved_by,
        modified_context=body.modified_context,
        new_budget_usd=body.new_budget_usd,
    )
    return {"instance_id": instance_id, "resolution": body.resolution}
