from __future__ import annotations

import contextlib

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from agentforge.api.deps import ApiDeps, get_deps, require
from agentforge.api.schemas import (
    ControlRequest,
    EventView,
    InstanceSummaryView,
    InstanceView,
    StepView,
)
from agentforge.core.auth import Principal, Scope
from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.events.types import dump_event
from agentforge.core.pubsub import InstanceStream
from agentforge.exceptions import ConfigurationError

router = APIRouter(prefix="/api/v1/instances", tags=["instances"])

_READ = require(Scope.INSTANCES_READ, Scope.INSTANCES_WRITE)
_WRITE = require(Scope.INSTANCES_WRITE)


def to_view(inst: WorkflowInstance) -> InstanceView:
    return InstanceView(
        instance_id=inst.instance_id,
        tenant_id=inst.tenant_id,
        workflow_id=inst.workflow_id,
        workflow_version=inst.workflow_version,
        status=inst.status.value,
        version=inst.version,
        context=inst.context,
        steps=[
            StepView(
                step_id=s.step_id,
                status=s.status.value,
                attempts=s.attempts,
                model_used=s.model_used,
                cost_usd=s.cost_usd,
                error_type=s.error_type,
                error_message=s.error_message,
            )
            for s in inst.step_states.values()
        ],
        cost_accumulated_usd=inst.cost_accumulated_usd,
        budget_limit_usd=inst.budget_limit_usd,
        escalations=[e.model_dump(mode="json") for e in inst.escalations],
        side_effects=[s.model_dump(mode="json") for s in inst.side_effects],
        created_at=inst.created_at,
        updated_at=inst.updated_at,
        completed_at=inst.completed_at,
    )


async def _load(deps: ApiDeps, instance_id: str, tenant_id: str) -> WorkflowInstance:
    inst = await deps.instances.get_instance(instance_id, tenant_id=tenant_id)
    if inst is None:
        raise ConfigurationError(f"instance {instance_id} not found")
    return inst


@router.get("")
async def list_instances(  # noqa: PLR0917 - FastAPI query params must be signature args
    status: list[str] | None = Query(None, description="filter by workflow status"),
    workflow_id: str | None = Query(None, description="filter by workflow id"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_READ),
) -> list[InstanceSummaryView]:
    rows = await deps.events.list_instances(
        principal.tenant_id,
        statuses=status,
        workflow_id=workflow_id,
        limit=limit,
        offset=offset,
    )
    return [
        InstanceSummaryView(
            instance_id=r.instance_id,
            workflow_id=r.workflow_id,
            workflow_version=r.workflow_version,
            status=r.status,
            cost_accumulated_usd=r.cost_accumulated_usd,
            budget_limit_usd=r.budget_limit_usd,
            next_wakeup_at=r.next_wakeup_at,
            created_at=r.created_at,
            updated_at=r.updated_at,
            completed_at=r.completed_at,
        )
        for r in rows
    ]


@router.get("/{instance_id}")
async def get_instance(
    instance_id: str,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_READ),
) -> InstanceView:
    return to_view(await _load(deps, instance_id, principal.tenant_id))


@router.get("/{instance_id}/events")
async def get_events(
    instance_id: str,
    after: int = Query(0, ge=0),
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_READ),
) -> list[EventView]:
    events = await deps.events.load(instance_id, principal.tenant_id, after=after)
    return [
        EventView(
            sequence=e.sequence,
            event_type=e.event_type,
            occurred_at=e.occurred_at,
            payload=dump_event(e),
        )
        for e in events
    ]


@router.get("/{instance_id}/history")
async def get_history(
    instance_id: str,
    at_version: int = Query(..., ge=1),
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_READ),
) -> InstanceView:
    inst = await deps.events.state_at(instance_id, principal.tenant_id, at_version)
    if inst is None:
        raise ConfigurationError(f"instance {instance_id} has no state at v{at_version}")
    return to_view(inst)


@router.post("/{instance_id}/pause")
async def pause(
    instance_id: str,
    body: ControlRequest,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_WRITE),
) -> InstanceView:
    await deps.control.pause(instance_id, principal.tenant_id, by=body.actor)
    return to_view(await _load(deps, instance_id, principal.tenant_id))


@router.post("/{instance_id}/resume")
async def resume(
    instance_id: str,
    body: ControlRequest,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_WRITE),
) -> InstanceView:
    await deps.control.resume(instance_id, principal.tenant_id, by=body.actor)
    return to_view(await _load(deps, instance_id, principal.tenant_id))


@router.post("/{instance_id}/abort")
async def abort(
    instance_id: str,
    body: ControlRequest,
    deps: ApiDeps = Depends(get_deps),
    principal: Principal = Depends(_WRITE),
) -> InstanceView:
    await deps.control.abort(instance_id, principal.tenant_id, by=body.actor)
    return to_view(await _load(deps, instance_id, principal.tenant_id))


@router.websocket("/{instance_id}/stream")
async def stream(websocket: WebSocket, instance_id: str) -> None:
    deps: ApiDeps = websocket.app.state.deps
    try:
        principal = await deps.auth.authenticate(
            api_key=websocket.query_params.get("api_key") or websocket.headers.get("x-api-key"),
            bearer=websocket.query_params.get("token"),
        )
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401)
        return

    inst = await deps.instances.get_instance(instance_id, tenant_id=principal.tenant_id)
    if inst is None or deps.redis is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    await websocket.send_json(to_view(inst).model_dump(mode="json"))
    stream_iter = InstanceStream(deps.redis).subscribe(instance_id)
    try:
        async for update in stream_iter:
            await websocket.send_json(update)
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(Exception):
            await stream_iter.aclose()
