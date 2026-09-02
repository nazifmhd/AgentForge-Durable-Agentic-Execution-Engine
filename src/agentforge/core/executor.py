"""``StepExecutor`` — run one step attempt with a timeout and classify the result.

It does not touch the event log; it returns a :class:`StepOutcome` the driver
turns into events. Keeping it side-effect-free (w.r.t. persistence) makes retry
and recovery reasoning simple: re-running an attempt is always safe here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from agentforge.core.runners import CostEntry, StepContext, StepRegistry
from agentforge.exceptions import RETRYABLE_BY_NAME, AgentForgeError


@dataclass(frozen=True, slots=True)
class StepSuccess:
    output: dict[str, Any]
    model_used: str | None
    charges: list[CostEntry] = field(default_factory=list)
    ok: Literal[True] = True


@dataclass(frozen=True, slots=True)
class StepFailure:
    error_type: str
    error_message: str
    retryable: bool
    charges: list[CostEntry] = field(default_factory=list)
    ok: Literal[False] = False


StepOutcome = StepSuccess | StepFailure


def _classify(exc: BaseException) -> bool:
    if isinstance(exc, AgentForgeError):
        return exc.retryable
    return type(exc).__name__ in RETRYABLE_BY_NAME


class StepExecutor:
    def __init__(self, registry: StepRegistry) -> None:
        self._registry = registry

    async def run_attempt(self, ctx: StepContext, *, timeout_seconds: int) -> StepOutcome:
        runner = self._registry.get(ctx.agent_type)
        try:
            result = await asyncio.wait_for(runner.run(ctx), timeout=timeout_seconds)
        except TimeoutError:
            return StepFailure(
                error_type="StepTimeoutError",
                error_message=f"step exceeded {timeout_seconds}s",
                retryable=True,
                charges=ctx.charges,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - deliberately broad; classified below
            return StepFailure(
                error_type=type(exc).__name__,
                error_message=str(exc) or type(exc).__name__,
                retryable=_classify(exc),
                charges=ctx.charges,
            )
        return StepSuccess(
            output=result.output,
            model_used=result.model_used,
            charges=ctx.charges,
        )
