"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentforge.api.deps import ApiDeps
from agentforge.api.errors import install_error_handlers
from agentforge.api.routes import (
    dead_letters,
    escalations,
    instances,
    system,
    webhooks,
    workflows,
)
from agentforge.config import settings
from agentforge.logging import get_logger
from agentforge.observability import configure_observability, instrument_fastapi

log = get_logger("api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_observability()
    if not hasattr(app.state, "deps"):
        from agentforge.bootstrap import build_api_deps

        app.state.deps = build_api_deps()
        app.state._owns_deps = True
    log.info("api_started")
    yield
    if getattr(app.state, "_owns_deps", False):
        from agentforge.db import dispose_engine
        from agentforge.redis_client import close_redis

        await dispose_engine()
        await close_redis()
    log.info("api_stopped")


def create_app(deps: ApiDeps | None = None) -> FastAPI:
    app = FastAPI(
        title="AgentForge",
        version="0.1.0",
        summary="Durable agentic execution engine",
        lifespan=_lifespan,
    )
    if deps is not None:
        app.state.deps = deps

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    install_error_handlers(app)
    for module in (workflows, instances, escalations, dead_letters, webhooks, system):
        app.include_router(module.router)

    instrument_fastapi(app)
    return app
