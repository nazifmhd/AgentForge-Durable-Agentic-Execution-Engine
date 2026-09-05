from __future__ import annotations

import jwt
from tests.api.conftest import TENANT, ApiHarness
from tests.factories import linear_workflow

from agentforge.config import settings
from agentforge.core.auth import Scope

WF = linear_workflow(2, workflow_id="sales", name="Sales").model_dump(mode="json")
APPROVAL_WF = {
    "workflow_id": "appr",
    "name": "Appr",
    "version": "1.0.0",
    "steps": [
        {"step_id": "a", "name": "A", "agent_type": "executor_agent"},
        {
            "step_id": "b",
            "name": "B",
            "agent_type": "executor_agent",
            "dependencies": ["a"],
            "requires_approval": True,
        },
    ],
}


def _h(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


# --- auth ----------------------------------------------------------
async def test_unauthenticated_is_401(api: ApiHarness) -> None:
    r = await api.client.get("/api/v1/escalations")
    assert r.status_code == 401


async def test_wrong_scope_is_403(api: ApiHarness) -> None:
    key = api.make_key(Scope.INSTANCES_READ)
    r = await api.client.post("/api/v1/workflows", json=WF, headers=_h(key))
    assert r.status_code == 403


async def test_jwt_auth_works(api: ApiHarness) -> None:
    token = jwt.encode(
        {"sub": "u1", "tenant": TENANT, "scopes": [Scope.WORKFLOWS_READ]},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    r = await api.client.get("/api/v1/workflows", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


# --- workflows + execute -----------------------------------------
async def test_register_then_execute_then_get(api: ApiHarness) -> None:
    key = api.make_key(Scope.ADMIN)

    r = await api.client.post("/api/v1/workflows", json=WF, headers=_h(key))
    assert r.status_code == 201

    r = await api.client.post(
        "/api/v1/workflows/Sales/execute",
        json={"context": {"lead": "x"}, "budget_limit_usd": 5.0},
        headers=_h(key),
    )
    assert r.status_code == 202
    instance_id = r.json()["instance_id"]

    r = await api.client.get(f"/api/v1/instances/{instance_id}", headers=_h(key))
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert r.json()["context"] == {"lead": "x"}

    await api.drive_all()
    r = await api.client.get(f"/api/v1/instances/{instance_id}", headers=_h(key))
    assert r.json()["status"] == "completed"

    r = await api.client.get(f"/api/v1/instances/{instance_id}/events", headers=_h(key))
    assert r.status_code == 200
    assert r.json()[0]["event_type"] == "InstanceCreated"

    r = await api.client.get(
        f"/api/v1/instances/{instance_id}/history?at_version=1", headers=_h(key)
    )
    assert r.json()["version"] == 1
    assert r.json()["status"] == "pending"


async def test_list_instances_filters_by_status_and_tenant(api: ApiHarness) -> None:
    key = api.make_key(Scope.ADMIN)
    await api.client.post("/api/v1/workflows", json=WF, headers=_h(key))
    ids = []
    for _ in range(3):
        r = await api.client.post("/api/v1/workflows/Sales/execute", json={}, headers=_h(key))
        ids.append(r.json()["instance_id"])
    await api.drive_all()  # all -> completed

    r = await api.client.get("/api/v1/instances", headers=_h(key))
    assert r.status_code == 200
    body = r.json()
    assert {row["instance_id"] for row in body} == set(ids)
    assert all(row["status"] == "completed" for row in body)

    r = await api.client.get("/api/v1/instances?status=completed&limit=2", headers=_h(key))
    assert len(r.json()) == 2
    r = await api.client.get("/api/v1/instances?status=running", headers=_h(key))
    assert r.json() == []

    other = api.make_key(Scope.ADMIN, tenant="someone-else")
    r = await api.client.get("/api/v1/instances", headers=_h(other))
    assert r.json() == []


async def test_tenant_isolation(api: ApiHarness) -> None:
    a_key = api.make_key(Scope.ADMIN, tenant="tenant-a")
    b_key = api.make_key(Scope.ADMIN, tenant="tenant-b")
    await api.client.post("/api/v1/workflows", json=WF, headers=_h(a_key))
    r = await api.client.post("/api/v1/workflows/Sales/execute", json={}, headers=_h(a_key))
    instance_id = r.json()["instance_id"]

    r = await api.client.get(f"/api/v1/instances/{instance_id}", headers=_h(b_key))
    assert r.status_code == 404


# --- control ---------------------------------------------------
async def test_pause_and_resume(api: ApiHarness) -> None:
    key = api.make_key(Scope.ADMIN)
    await api.client.post("/api/v1/workflows", json=WF, headers=_h(key))
    r = await api.client.post("/api/v1/workflows/Sales/execute", json={}, headers=_h(key))
    iid = r.json()["instance_id"]

    r = await api.client.post(
        f"/api/v1/instances/{iid}/pause", json={"actor": "ops"}, headers=_h(key)
    )
    assert r.status_code == 200
    assert r.json()["status"] == "paused"

    r = await api.client.post(
        f"/api/v1/instances/{iid}/resume", json={"actor": "ops"}, headers=_h(key)
    )
    assert r.json()["status"] == "running"


# --- escalations ----------------------------------------------
async def test_escalation_list_and_resolve(api: ApiHarness) -> None:
    key = api.make_key(Scope.ADMIN)
    await api.client.post("/api/v1/workflows", json=APPROVAL_WF, headers=_h(key))
    r = await api.client.post("/api/v1/workflows/Appr/execute", json={}, headers=_h(key))
    iid = r.json()["instance_id"]
    await api.drive_all()

    r = await api.client.get("/api/v1/escalations", headers=_h(key))
    assert r.status_code == 200
    escalations = r.json()
    assert len(escalations) == 1
    assert escalations[0]["step_id"] == "b"

    r = await api.client.post(
        f"/api/v1/escalations/{escalations[0]['escalation_id']}/resolve",
        json={"resolution": "approve", "resolved_by": "alice"},
        headers=_h(key),
    )
    assert r.status_code == 200

    await api.drive_all()
    r = await api.client.get(f"/api/v1/instances/{iid}", headers=_h(key))
    assert r.json()["status"] == "completed"


# --- rate limiting -------------------------------------------
async def test_execute_is_rate_limited(api: ApiHarness) -> None:
    key = api.make_key(Scope.ADMIN)
    await api.client.post("/api/v1/workflows", json=WF, headers=_h(key))
    settings_limit = settings.execute_rate_limit_per_minute

    hits = [
        (
            await api.client.post("/api/v1/workflows/Sales/execute", json={}, headers=_h(key))
        ).status_code
        for _ in range(settings_limit + 3)
    ]
    assert hits.count(202) == settings_limit
    assert 429 in hits


# --- system --------------------------------------------------
async def test_health_and_metrics(api: ApiHarness) -> None:
    assert (await api.client.get("/health/live")).status_code == 200
    r = await api.client.get("/health/ready")
    assert r.status_code in (200, 503)  # in-memory events has no _sm; may 503
    r = await api.client.get("/metrics")
    assert r.status_code == 200
    assert b"python_info" in r.content or b"# HELP" in r.content
