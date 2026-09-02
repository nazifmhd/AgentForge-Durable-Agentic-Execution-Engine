"""Map engine exceptions onto HTTP responses."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentforge.core.auth import AuthError, ForbiddenError
from agentforge.exceptions import (
    AgentForgeError,
    BudgetExceededError,
    ConfigurationError,
    ConflictError,
    WorkflowDefinitionError,
)


def _json(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthError)
    async def _auth(_r: Request, exc: AuthError) -> JSONResponse:
        return _json(401, "unauthenticated", str(exc) or "authentication required")

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_r: Request, exc: ForbiddenError) -> JSONResponse:
        return _json(403, "forbidden", str(exc) or "insufficient scope")

    @app.exception_handler(WorkflowDefinitionError)
    async def _defn(_r: Request, exc: WorkflowDefinitionError) -> JSONResponse:
        return _json(422, "invalid_workflow", str(exc))

    @app.exception_handler(ConfigurationError)
    async def _config(_r: Request, exc: ConfigurationError) -> JSONResponse:
        return _json(404, "not_found", str(exc))

    @app.exception_handler(ConflictError)
    async def _conflict(_r: Request, exc: ConflictError) -> JSONResponse:
        return _json(409, "conflict", str(exc))

    @app.exception_handler(BudgetExceededError)
    async def _budget(_r: Request, exc: BudgetExceededError) -> JSONResponse:
        return _json(402, "budget_exceeded", str(exc))

    @app.exception_handler(AgentForgeError)
    async def _generic(_r: Request, exc: AgentForgeError) -> JSONResponse:
        return _json(400, "engine_error", str(exc))
