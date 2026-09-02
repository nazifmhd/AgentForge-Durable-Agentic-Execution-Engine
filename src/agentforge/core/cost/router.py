"""``CostAwareRouter`` — pick the cheapest capable model within budget (ADR-0008).

Filter → project cost → cheapest wins → **refuse if it doesn't fit the budget**.
The refusal is a ``BudgetExceededError`` the step surfaces, which the driver turns
into a ``COST_THRESHOLD`` escalation rather than an overspend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentforge.core.cost.budget import BudgetView
from agentforge.core.cost.registry import ModelConfig, ModelRegistry
from agentforge.core.domain.enums import CostTier
from agentforge.exceptions import BudgetExceededError, NoEligibleModelError
from agentforge.logging import get_logger

log = get_logger("cost_router")


@dataclass(frozen=True, slots=True)
class RouteRequest:
    cost_tier: CostTier = CostTier.STANDARD
    task_type: str = "general"
    estimated_input_tokens: int = 1000
    expected_output_tokens: int = 500
    needs_tools: bool = False
    needs_vision: bool = False
    reliability_floor: float = 0.0
    max_latency_ms: int | None = None
    exclude: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RouteDecision:
    model_key: str
    model: ModelConfig
    projected_cost_usd: float
    projected_input_tokens: int
    projected_output_tokens: int
    fallback_chain: list[str] = field(default_factory=list)


class CostAwareRouter:
    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    def _eligible(self, req: RouteRequest) -> list[ModelConfig]:
        out: list[ModelConfig] = []
        for m in self._registry.for_tier(req.cost_tier):
            if m.key in req.exclude:
                continue
            if req.needs_tools and not m.supports_tools:
                continue
            if req.needs_vision and not m.supports_vision:
                continue
            if m.reliability_score < req.reliability_floor:
                continue
            if req.max_latency_ms is not None and m.avg_latency_ms > req.max_latency_ms:
                continue
            if req.estimated_input_tokens + req.expected_output_tokens > m.context_window:
                continue
            if req.expected_output_tokens > m.max_output_tokens:
                continue
            out.append(m)
        return out

    def select(self, req: RouteRequest, *, budget: BudgetView | None = None) -> RouteDecision:
        candidates = self._eligible(req)
        if not candidates:
            raise NoEligibleModelError(
                f"no model satisfies tier={req.cost_tier} "
                f"tools={req.needs_tools} vision={req.needs_vision} "
                f"reliability>={req.reliability_floor}"
            )

        scored = sorted(
            (
                (
                    m,
                    m.cost_usd(req.estimated_input_tokens, req.expected_output_tokens),
                )
                for m in candidates
            ),
            key=lambda pair: pair[1],
        )
        model, projected = scored[0]

        if budget is not None and not budget.allows(projected):
            raise BudgetExceededError(
                f"projected ${projected:.4f} for {model.key} exceeds remaining "
                f"budget ${budget.tightest_remaining():.4f}"
            )

        chain = [
            k
            for k in self._registry.fallback_chain(req.cost_tier)
            if k != model.key and k not in req.exclude
        ]
        return RouteDecision(
            model_key=model.key,
            model=model,
            projected_cost_usd=projected,
            projected_input_tokens=req.estimated_input_tokens,
            projected_output_tokens=req.expected_output_tokens,
            fallback_chain=chain,
        )
