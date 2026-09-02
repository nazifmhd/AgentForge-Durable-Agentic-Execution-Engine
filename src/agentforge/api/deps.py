"""Shared FastAPI dependencies: the wired services, auth, and rate limiting."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request

from agentforge.api.middleware.rate_limit import RateLimiter
from agentforge.config import settings
from agentforge.core.auth import AuthService, ForbiddenError, Principal
from agentforge.core.control import InstanceControl
from agentforge.core.dead_letter import DeadLetterService
from agentforge.core.escalation import EscalationController
from agentforge.core.instances import InstanceService
from agentforge.core.persistence.definition_repo import DefinitionRepository
from agentforge.core.persistence.event_store import EventStore


@dataclass(slots=True)
class ApiDeps:
    auth: AuthService
    rate_limiter: RateLimiter
    definitions: DefinitionRepository
    instances: InstanceService
    events: EventStore
    escalations: EscalationController
    dead_letters: DeadLetterService
    control: InstanceControl
    redis: Any | None = None


def get_deps(request: Request) -> ApiDeps:
    deps: ApiDeps = request.app.state.deps
    return deps


async def current_principal(request: Request, deps: ApiDeps = Depends(get_deps)) -> Principal:
    return await deps.auth.authenticate(
        api_key=request.headers.get("x-api-key"),
        bearer=_bearer(request.headers.get("authorization")),
    )


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def require(*scopes: str) -> Callable[..., Awaitable[Principal]]:
    async def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not any(principal.has(s) for s in scopes):
            raise ForbiddenError(f"need one of: {', '.join(scopes)}")
        return principal

    return _dep


def rate_limited(bucket: str, per_minute: int | None = None) -> Callable[..., Awaitable[None]]:
    async def _dep(
        request: Request,
        deps: ApiDeps = Depends(get_deps),
        principal: Principal = Depends(current_principal),
    ) -> None:
        limit = per_minute if per_minute is not None else settings.rate_limit_per_minute
        decision = await deps.rate_limiter.check(principal.tenant_id, bucket, limit)
        request.state.rate = decision
        if not decision.allowed:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(decision.reset_in)},
            )

    return _dep
