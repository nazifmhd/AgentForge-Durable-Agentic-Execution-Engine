from __future__ import annotations

import asyncio

from agentforge.core.executor import StepExecutor, StepFailure, StepSuccess
from agentforge.core.runners import (
    FunctionRunner,
    StepContext,
    StepRegistry,
    StepResult,
)
from agentforge.exceptions import ConfigurationError, RateLimitError


def _ctx(agent_type: str = "t", attempt: int = 1) -> StepContext:
    return StepContext(
        instance_id="i",
        tenant_id="t",
        step_id="s",
        agent_type=agent_type,
        attempt=attempt,
        inputs={},
        instance_context={},
    )


async def test_success_carries_output_and_charges() -> None:
    async def fn(ctx: StepContext) -> StepResult:
        ctx.charge(0.02, model="m", tokens_input=10, tokens_output=5)
        return StepResult(output={"v": 1}, model_used="m")

    reg = StepRegistry()
    reg.register("t", FunctionRunner(fn))
    out = await StepExecutor(reg).run_attempt(_ctx(), timeout_seconds=5)

    assert isinstance(out, StepSuccess)
    assert out.output == {"v": 1}
    assert out.model_used == "m"
    assert out.charges[0].amount_usd == 0.02


async def test_timeout_is_retryable_failure() -> None:
    async def fn(ctx: StepContext) -> dict:
        await asyncio.sleep(10)
        return {}

    reg = StepRegistry()
    reg.register("t", FunctionRunner(fn))
    out = await StepExecutor(reg).run_attempt(_ctx(), timeout_seconds=1)

    assert isinstance(out, StepFailure)
    assert out.error_type == "StepTimeoutError"
    assert out.retryable is True


async def test_domain_error_uses_its_retryable_flag() -> None:
    async def fn(ctx: StepContext) -> dict:
        raise RateLimitError("429")

    reg = StepRegistry()
    reg.register("t", FunctionRunner(fn))
    out = await StepExecutor(reg).run_attempt(_ctx(), timeout_seconds=5)

    assert isinstance(out, StepFailure)
    assert out.error_type == "RateLimitError"
    assert out.retryable is True


async def test_unknown_error_is_non_retryable_and_keeps_partial_charges() -> None:
    async def fn(ctx: StepContext) -> dict:
        ctx.charge(0.01)
        raise ValueError("bad data")

    reg = StepRegistry()
    reg.register("t", FunctionRunner(fn))
    out = await StepExecutor(reg).run_attempt(_ctx(), timeout_seconds=5)

    assert isinstance(out, StepFailure)
    assert out.retryable is False
    assert out.charges[0].amount_usd == 0.01


async def test_missing_runner_raises() -> None:
    import pytest

    with pytest.raises(ConfigurationError):
        await StepExecutor(StepRegistry()).run_attempt(_ctx("nope"), timeout_seconds=5)
