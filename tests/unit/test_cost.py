from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.factories import linear_workflow  # noqa: F401 - keeps factories importable

from agentforge.core.cost.budget import (
    BudgetService,
    BudgetView,
    InMemoryBudgetLedger,
)
from agentforge.core.cost.estimator import estimate_message_tokens, estimate_text_tokens
from agentforge.core.cost.registry import ModelRegistry
from agentforge.core.cost.router import CostAwareRouter, RouteRequest
from agentforge.core.domain.enums import CostTier
from agentforge.exceptions import (
    BudgetExceededError,
    ConfigurationError,
    NoEligibleModelError,
)

REGISTRY_YAML = """
models:
  cheapo:
    model_id: cheapo-1
    provider: fake
    input_per_mtok: 0.10
    output_per_mtok: 0.40
    context_window: 100000
    max_output_tokens: 4096
    avg_latency_ms: 300
    reliability_score: 0.90
    supports_tools: true
    supports_vision: false
    tiers: [cheap, standard]
  midtier:
    model_id: mid-1
    provider: fake
    input_per_mtok: 1.00
    output_per_mtok: 5.00
    context_window: 200000
    max_output_tokens: 8192
    avg_latency_ms: 1200
    reliability_score: 0.97
    supports_tools: true
    supports_vision: true
    tiers: [standard, premium]
  premo:
    model_id: prem-1
    provider: fake
    input_per_mtok: 5.00
    output_per_mtok: 25.00
    context_window: 200000
    max_output_tokens: 16384
    avg_latency_ms: 3000
    reliability_score: 0.99
    supports_tools: true
    supports_vision: true
    tiers: [premium, critical]
fallback_chains:
  standard: [cheapo, midtier, premo]
"""


def _registry() -> ModelRegistry:
    import yaml

    return ModelRegistry.from_dict(yaml.safe_load(REGISTRY_YAML))


# --- estimator ---------------------------------------------------------
def test_text_estimate_is_ceil_div_four() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("abcde") == 2


def test_message_estimate_includes_system_and_overhead() -> None:
    n = estimate_message_tokens([{"role": "user", "content": "hello there"}], system="be brief")
    assert n > estimate_text_tokens("hello there")


# --- registry --------------------------------------------------------
def test_registry_for_tier_sorted_by_cost() -> None:
    reg = _registry()
    keys = [m.key for m in reg.for_tier(CostTier.STANDARD)]
    assert keys == ["cheapo", "midtier"]  # premo isn't in the standard tier


def test_registry_unknown_model_raises() -> None:
    with pytest.raises(ConfigurationError):
        _registry().get("ghost")


def test_registry_rejects_bad_fallback_reference() -> None:
    import yaml

    bad = yaml.safe_load(REGISTRY_YAML)
    bad["fallback_chains"]["standard"] = ["cheapo", "nonexistent"]
    with pytest.raises(ConfigurationError, match="unknown model"):
        ModelRegistry.from_dict(bad)


def test_real_config_file_loads() -> None:
    reg = ModelRegistry.from_path("config/models.yaml")
    assert reg.get("claude-sonnet-5").model_id == "claude-sonnet-5"
    assert "claude-haiku-4-5" in reg.fallback_chain(CostTier.STANDARD)


# --- router --------------------------------------------------------
def test_router_picks_cheapest_capable_model() -> None:
    router = CostAwareRouter(_registry())
    d = router.select(RouteRequest(cost_tier=CostTier.STANDARD))
    assert d.model_key == "cheapo"
    assert d.fallback_chain == ["midtier", "premo"]


def test_router_vision_requirement_filters_out_cheapest() -> None:
    router = CostAwareRouter(_registry())
    d = router.select(RouteRequest(cost_tier=CostTier.STANDARD, needs_vision=True))
    assert d.model_key == "midtier"


def test_router_context_fit_filters() -> None:
    router = CostAwareRouter(_registry())
    d = router.select(
        RouteRequest(
            cost_tier=CostTier.STANDARD,
            estimated_input_tokens=150_000,
            expected_output_tokens=1000,
        )
    )
    assert d.model_key == "midtier"  # cheapo's 100k window can't hold it


def test_router_reliability_floor() -> None:
    router = CostAwareRouter(_registry())
    d = router.select(RouteRequest(cost_tier=CostTier.STANDARD, reliability_floor=0.95))
    assert d.model_key == "midtier"


def test_router_no_eligible_model() -> None:
    router = CostAwareRouter(_registry())
    with pytest.raises(NoEligibleModelError):
        router.select(RouteRequest(cost_tier=CostTier.STANDARD, reliability_floor=0.999))


def test_router_refuses_when_over_budget() -> None:
    router = CostAwareRouter(_registry())
    tiny = BudgetView(remaining_workflow_usd=0.0000001, remaining_tenant_daily_usd=None)
    with pytest.raises(BudgetExceededError):
        router.select(
            RouteRequest(
                cost_tier=CostTier.STANDARD,
                estimated_input_tokens=10_000,
                expected_output_tokens=5_000,
            ),
            budget=tiny,
        )


# --- budget --------------------------------------------------------
def test_budget_view_allows() -> None:
    assert BudgetView(1.0, 2.0).allows(0.5) is True
    assert BudgetView(1.0, 2.0).allows(1.5) is False
    assert BudgetView(None, None).allows(999) is True


async def test_budget_service_combines_workflow_and_tenant() -> None:
    from agentforge.core.domain.instance import WorkflowInstance

    ledger = InMemoryBudgetLedger()
    svc = BudgetService(ledger, tenant_daily_limit_usd=10.0)
    now = datetime(2026, 9, 1, tzinfo=UTC)
    await svc.record_spend("t1", 7.0, now=now)

    inst = WorkflowInstance(
        instance_id="i",
        tenant_id="t1",
        workflow_id="w",
        workflow_version="1.0.0",
        budget_limit_usd=2.0,
        cost_accumulated_usd=0.5,
    )
    view = await svc.view(inst, now=now)
    assert view.remaining_workflow_usd == pytest.approx(1.5)
    assert view.remaining_tenant_daily_usd == pytest.approx(3.0)
    assert view.tightest_remaining() == pytest.approx(1.5)
