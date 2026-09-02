"""Step runners — the pluggable unit of work behind a workflow step.

A ``WorkflowStep.agent_type`` names a runner registered in a :class:`StepRegistry`.
Phase 2 ships trivial runners for tests and glue steps; the LangGraph agent
runtime (Phase 7) registers real ones.

A runner receives a :class:`StepContext` (its resolved inputs + a cost sink) and
returns a :class:`StepResult`. It must not touch the event log or the database —
the executor turns its result into events.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from agentforge.core.domain.enums import CostTier
from agentforge.core.ports import SYSTEM_CLOCK, Clock
from agentforge.exceptions import ConfigurationError

if TYPE_CHECKING:
    from agentforge.core.cost.budget import BudgetView
    from agentforge.core.llm_client import LLMClient
    from agentforge.core.side_effects import EffectOutcome, SideEffectGuard
    from agentforge.integrations.actions.base import EffectResult
    from agentforge.integrations.llm.base import LLMMessage, LLMResponse


@dataclass
class CostEntry:
    amount_usd: float
    model: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0


@dataclass
class StepContext:
    instance_id: str
    tenant_id: str
    step_id: str
    agent_type: str
    attempt: int
    inputs: dict[str, Any]
    instance_context: dict[str, Any]
    clock: Clock = SYSTEM_CLOCK
    guard: SideEffectGuard | None = None
    llm_client: LLMClient | None = None
    budget: BudgetView | None = None
    _charges: list[CostEntry] = field(default_factory=list)
    _effects: list[EffectOutcome] = field(default_factory=list)

    def charge(
        self,
        amount_usd: float,
        *,
        model: str | None = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
    ) -> None:
        """Record a billable call. The executor emits one ``CostCharged`` per entry."""
        self._charges.append(CostEntry(amount_usd, model, tokens_input, tokens_output))

    async def execute_effect(
        self,
        effect_name: str,
        params: dict[str, Any],
        *,
        provider: str = "noop",
    ) -> EffectResult:
        """Perform an external side effect exactly-once through the guard (ADR-0003)."""
        if self.guard is None:
            raise ConfigurationError("step attempted a side effect but no SideEffectGuard is wired")
        outcome = await self.guard.execute(
            instance_id=self.instance_id,
            tenant_id=self.tenant_id,
            step_id=self.step_id,
            effect_name=effect_name,
            params=params,
            provider_name=provider,
        )
        self._effects.append(outcome)
        return outcome.result

    async def llm(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tier: CostTier | str = "standard",
        max_tokens: int = 4096,
        task_type: str = "general",
        expected_output_tokens: int | None = None,
        reliability_floor: float = 0.0,
    ) -> LLMResponse:
        """Cost-aware LLM call: routes to the cheapest capable model within budget,
        charges the actual cost, and returns the completion."""
        if self.llm_client is None:
            raise ConfigurationError("step attempted an LLM call but no LLMClient is wired")
        completion = await self.llm_client.complete(
            messages=messages,
            system=system,
            tools=tools,
            tier=CostTier(tier) if isinstance(tier, str) else tier,
            max_tokens=max_tokens,
            task_type=task_type or self.agent_type,
            expected_output_tokens=expected_output_tokens,
            reliability_floor=reliability_floor,
            budget=self.budget,
        )
        r = completion.response
        self.charge(
            completion.cost_usd,
            model=r.model_id,
            tokens_input=r.tokens_input,
            tokens_output=r.tokens_output,
        )
        return r

    @property
    def charges(self) -> list[CostEntry]:
        return list(self._charges)

    @property
    def effects(self) -> list[EffectOutcome]:
        return list(self._effects)


@dataclass(frozen=True, slots=True)
class StepResult:
    output: dict[str, Any]
    model_used: str | None = None


class StepRunner(Protocol):
    async def run(self, ctx: StepContext) -> StepResult: ...


class FunctionRunner:
    """Adapt ``async fn(ctx) -> dict | StepResult`` to a runner."""

    def __init__(
        self,
        fn: Callable[[StepContext], Awaitable[dict[str, Any] | StepResult]],
    ) -> None:
        self._fn = fn

    async def run(self, ctx: StepContext) -> StepResult:
        out = await self._fn(ctx)
        return out if isinstance(out, StepResult) else StepResult(output=out)


class EchoRunner:
    """Returns its resolved inputs — useful for gate / pass-through steps and tests."""

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(output={"inputs": ctx.inputs, "attempt": ctx.attempt})


class StepRegistry:
    def __init__(self) -> None:
        self._runners: dict[str, StepRunner] = {}

    def register(self, agent_type: str, runner: StepRunner) -> None:
        self._runners[agent_type] = runner

    def get(self, agent_type: str) -> StepRunner:
        try:
            return self._runners[agent_type]
        except KeyError:
            raise ConfigurationError(
                f"no step runner registered for agent_type {agent_type!r}"
            ) from None

    def __contains__(self, agent_type: str) -> bool:
        return agent_type in self._runners


def default_registry() -> StepRegistry:
    reg = StepRegistry()
    reg.register("echo_agent", EchoRunner())
    return reg
