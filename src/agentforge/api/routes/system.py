from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from agentforge import __version__
from agentforge.api.deps import ApiDeps, get_deps

router = APIRouter(tags=["system"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/health/ready")
async def ready(deps: ApiDeps = Depends(get_deps)) -> Response:
    checks: dict[str, str] = {}
    try:
        async with deps.events._sm() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"
    if deps.redis is not None:
        try:
            await deps.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {exc}"

    ok = all(v == "ok" for v in checks.values())
    import orjson

    return Response(
        content=orjson.dumps({"ready": ok, "checks": checks}),
        status_code=200 if ok else 503,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
