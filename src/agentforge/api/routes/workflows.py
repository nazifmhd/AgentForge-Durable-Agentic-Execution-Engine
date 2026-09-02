from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from agentforge.api.deps import ApiDeps, get_deps, rate_limited, require
from agentforge.api.schemas import ExecuteRequest, ExecuteResponse
from agentforge.config import settings
from agentforge.core.auth import Principal, Scope
from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import TriggerSource
from agentforge.exceptions import ConfigurationError

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

_WRITE_DEFN = require(Scope.WORKFLOWS_WRITE)
_READ_DEFN = require(Scope.WORKFLOWS_READ, Scope.WORKFLOWS_WRITE)
_EXECUTE = require(Scope.INSTANCES_WRITE)
_EXECUTE_RL = rate_limited("execute", settings.execute_rate_limit_per_minute)


@router.post("", status_code=201)
async def register_workflow(
    body: dict[str, Any],
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_WRITE_DEFN),
) -> dict[str, str]:
    definition = WorkflowDefinition.model_validate(body)
    await deps.definitions.register(definition, tenant_id=principal.tenant_id)
    return {
        "workflow_id": definition.workflow_id,
        "version": definition.version,
        "checksum": definition.checksum,
    }


@router.get("")
async def list_workflows(
    active_only: bool = False,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_READ_DEFN),
) -> list[dict[str, Any]]:
    defns = await deps.definitions.list(tenant_id=principal.tenant_id, active_only=active_only)
    return [d.model_dump(mode="json") for d in defns]


@router.get("/{workflow_id}/{version}")
async def get_workflow(
    workflow_id: str,
    version: str,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_READ_DEFN),
) -> dict[str, Any]:
    defn = await deps.definitions.get(workflow_id, version, tenant_id=principal.tenant_id)
    if defn is None:
        raise ConfigurationError(f"workflow {workflow_id} v{version} not found")
    return defn.model_dump(mode="json")


@router.post("/{name}/execute", status_code=202)
async def execute_workflow(
    name: str,
    body: ExecuteRequest,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_EXECUTE),
    _rl: None = Depends(_EXECUTE_RL),
) -> ExecuteResponse:
    kwargs: dict[str, Any] = {"tenant_id": principal.tenant_id}
    if body.version is not None:
        kwargs["workflow_id"] = name
        kwargs["version"] = body.version
    else:
        kwargs["name"] = name

    instance = await deps.instances.create_instance(
        context=body.context,
        budget_limit_usd=body.budget_limit_usd,
        trigger_source=TriggerSource.API,
        trigger_metadata={"priority": body.priority},
        **kwargs,
    )
    return ExecuteResponse(
        instance_id=instance.instance_id,
        workflow_id=instance.workflow_id,
        version=instance.workflow_version,
        status=instance.status.value,
    )
