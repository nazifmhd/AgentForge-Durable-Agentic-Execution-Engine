from __future__ import annotations

import pytest
import yaml
from tests.doubles import FakeLLMProvider

from agentforge.core.cost.budget import BudgetView
from agentforge.core.cost.registry import ModelRegistry
from agentforge.core.cost.router import CostAwareRouter
from agentforge.core.domain.enums import CostTier
from agentforge.core.llm_client import LLMClient
from agentforge.exceptions import BudgetExceededError, RateLimitError
from agentforge.integrations.llm.base import LLMMessage, LLMProviderRegistry

_YAML = """
models:
  a:
    model_id: a-1
    provider: p
    input_per_mtok: 0.10
    output_per_mtok: 0.40
    context_window: 100000
    max_output_tokens: 8192
    avg_latency_ms: 300
    reliability_score: 0.95
    tiers: [standard]
  b:
    model_id: b-1
    provider: p
    input_per_mtok: 1.00
    output_per_mtok: 4.00
    context_window: 100000
    max_output_tokens: 8192
    avg_latency_ms: 900
    reliability_score: 0.98
    tiers: [standard]
fallback_chains:
  standard: [a, b]
"""


def _client(provider: FakeLLMProvider) -> LLMClient:
    reg = ModelRegistry.from_dict(yaml.safe_load(_YAML))
    providers = LLMProviderRegistry()
    providers.register(provider)
    return LLMClient(CostAwareRouter(reg), reg, providers)


async def test_routes_to_cheapest_and_reports_cost() -> None:
    provider = FakeLLMProvider(name="p", tokens_in=1_000_000, tokens_out=1_000_000)
    client = _client(provider)
    out = await client.complete(
        messages=[LLMMessage(role="user", content="hi")], tier=CostTier.STANDARD
    )
    assert out.decision.model_key == "a"
    assert out.response.model_id == "a-1"
    assert out.cost_usd == pytest.approx(0.10 + 0.40)  # 1M in + 1M out at model a's rate
    assert out.models_tried == ["a"]


async def test_falls_back_to_next_model_on_retryable_error() -> None:
    provider = FakeLLMProvider(name="p", fail_times=1)  # model a fails once
    client = _client(provider)
    out = await client.complete(
        messages=[LLMMessage(role="user", content="hi")], tier=CostTier.STANDARD
    )
    assert out.models_tried == ["a", "b"]
    assert out.response.model_id == "b-1"


async def test_all_models_failing_raises_last_error() -> None:
    provider = FakeLLMProvider(name="p", fail_times=99)
    client = _client(provider)
    with pytest.raises(RateLimitError):
        await client.complete(
            messages=[LLMMessage(role="user", content="hi")], tier=CostTier.STANDARD
        )


async def test_budget_refusal_propagates_before_any_call() -> None:
    provider = FakeLLMProvider(name="p")
    client = _client(provider)
    with pytest.raises(BudgetExceededError):
        await client.complete(
            messages=[LLMMessage(role="user", content="x" * 4000)],
            tier=CostTier.STANDARD,
            expected_output_tokens=5000,
            budget=BudgetView(remaining_workflow_usd=1e-9, remaining_tenant_daily_usd=None),
        )
    assert provider.calls == []
