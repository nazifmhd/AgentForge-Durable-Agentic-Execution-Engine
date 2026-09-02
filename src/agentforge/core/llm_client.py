"""``LLMClient`` — cost-aware routing + provider dispatch + fallback.

An agent asks for a *tier* and a rough shape of the call; the client estimates
tokens, routes to the cheapest capable model within budget (ADR-0008), calls the
provider, and walks the tier's fallback chain on a retryable failure. It returns
the response plus the actual cost so the caller can charge it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agentforge.core.cost.budget import BudgetView
from agentforge.core.cost.estimator import estimate_message_tokens
from agentforge.core.cost.registry import ModelRegistry
from agentforge.core.cost.router import CostAwareRouter, RouteDecision, RouteRequest
from agentforge.core.domain.enums import CostTier
from agentforge.exceptions import (
    LLMError,
    LLMTimeoutError,
    MalformedOutputError,
    RateLimitError,
)
from agentforge.integrations.llm.base import (
    LLMMessage,
    LLMProviderRegistry,
    LLMRequest,
    LLMResponse,
)
from agentforge.logging import get_logger

log = get_logger("llm_client")

_RETRYABLE_LLM = (LLMTimeoutError, RateLimitError, MalformedOutputError)


@dataclass(frozen=True, slots=True)
class LLMCompletion:
    response: LLMResponse
    decision: RouteDecision
    cost_usd: float
    models_tried: list[str] = field(default_factory=list)


class LLMClient:
    def __init__(
        self,
        router: CostAwareRouter,
        registry: ModelRegistry,
        providers: LLMProviderRegistry,
    ) -> None:
        self._router = router
        self._registry = registry
        self._providers = providers

    async def complete(
        self,
        *,
        messages: Sequence[LLMMessage],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        tier: CostTier = CostTier.STANDARD,
        task_type: str = "general",
        needs_vision: bool = False,
        reliability_floor: float = 0.0,
        expected_output_tokens: int | None = None,
        budget: BudgetView | None = None,
    ) -> LLMCompletion:
        msg_dicts = [{"role": m.role, "content": m.content} for m in messages]
        est_input = estimate_message_tokens(msg_dicts, system=system, tools=tools)
        exp_output = expected_output_tokens or min(max_tokens, 1024)

        decision = self._router.select(
            RouteRequest(
                cost_tier=tier,
                task_type=task_type,
                estimated_input_tokens=est_input,
                expected_output_tokens=exp_output,
                needs_tools=bool(tools),
                needs_vision=needs_vision,
                reliability_floor=reliability_floor,
            ),
            budget=budget,
        )

        chain = [decision.model_key, *decision.fallback_chain]
        tried: list[str] = []
        last_exc: Exception | None = None
        for model_key in chain:
            model = self._registry.get(model_key)
            provider = self._providers.get(model.provider)
            tried.append(model_key)
            req = LLMRequest(
                model_id=model.model_id,
                messages=list(messages),
                system=system,
                tools=tools or [],
                max_tokens=max_tokens,
            )
            try:
                resp = await provider.complete(req)
            except _RETRYABLE_LLM as exc:
                last_exc = exc
                log.warning("llm_model_failed", model=model_key, error=str(exc))
                continue
            return LLMCompletion(
                response=resp,
                decision=decision,
                cost_usd=model.cost_usd(resp.tokens_input, resp.tokens_output),
                models_tried=tried,
            )

        raise last_exc or LLMError("every model in the fallback chain failed")
