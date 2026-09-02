"""Inbound webhooks — e.g. n8n triggering a workflow on a new event.

Authenticated with an API key like every other write route (n8n's HTTP node
sends the ``X-API-Key`` header). A flatter body than ``/execute`` to match what
webhook sources typically post.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from agentforge.api.deps import ApiDeps, get_deps, rate_limited, require
from agentforge.api.schemas import ExecuteResponse
from agentforge.config import settings
from agentforge.core.auth import Principal, Scope
from agentforge.core.domain.enums import TriggerSource

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

_WRITE = require(Scope.INSTANCES_WRITE)
_RL = rate_limited("execute", settings.execute_rate_limit_per_minute)


@router.post("/{workflow_name}", status_code=202)
async def trigger(
    workflow_name: str,
    body: dict[str, Any],
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_WRITE),
    _rl: None = Depends(_RL),
) -> ExecuteResponse:
    context = body.get("context", body)
    instance = await deps.instances.create_instance(
        tenant_id=principal.tenant_id,
        name=workflow_name,
        context=context,
        budget_limit_usd=body.get("budget_limit_usd"),
        trigger_source=TriggerSource.N8N,
        trigger_metadata={"source": "webhook"},
    )
    return ExecuteResponse(
        instance_id=instance.instance_id,
        workflow_id=instance.workflow_id,
        version=instance.workflow_version,
        status=instance.status.value,
    )
