"""Phase 7 — LangGraph agent runtime (planner / executor / validator / reflector)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    ScriptedLLMProvider,
    seed_instance,
)
from tests.factories import make_step

from agentforge.agents import (
    ExecutorAgent,
    FunctionTool,
    PlannerAgent,
    ReflectorAgent,
    ToolRegistry,
    ValidatorAgent,
    register_base_agents,
)
from agentforge.agents.executor_agent import _MAX_TOOL_CALLS
from agentforge.core.cost.budget import BudgetService, InMemoryBudgetLedger
from agentforge.core.cost.registry import ModelRegistry
from agentforge.core.cost.router import CostAwareRouter
from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.executor import StepExecutor
from agentforge.core.llm_client import LLMClient
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import StepContext, StepRegistry
from agentforge.integrations.llm.base import LLMProviderRegistry

T0 = datetime(2026, 9, 1, tzinfo=UTC)
TENANT = "tenant-1"

_YAML = """
models:
  m:
    model_id: m-1
    provider: p
    input_per_mtok: 1.0
    output_per_mtok: 4.0
    context_window: 100000
    max_output_tokens: 8192
    avg_latency_ms: 300
    reliability_score: 0.95
    tiers: [cheap, standard, premium]
fallback_chains:
  cheap: [m]
  standard: [m]
  premium: [m]
"""


def _client(provider: ScriptedLLMProvider) -> LLMClient:
    models = ModelRegistry.from_dict(yaml.safe_load(_YAML))
    providers = LLMProviderRegistry()
    providers.register(provider)
    return LLMClient(CostAwareRouter(models), models, providers)


def _ctx(provider: ScriptedLLMProvider, inputs: dict[str, Any], *, agent_type: str) -> StepContext:
    return StepContext(
        instance_id="inst-1",
        tenant_id=TENANT,
        step_id="s1",
        agent_type=agent_type,
        attempt=1,
        inputs=inputs,
        instance_context={},
        clock=FixedClock(T0),
        llm_client=_client(provider),
    )


async def test_planner_agent_produces_a_plan() -> None:
    plan_json = '[{"step": "collect data", "rationale": "need inputs", "depends_on": []}]'
    provider = ScriptedLLMProvider(
        ["- subproblem one\n- subproblem two", plan_json, "order looks fine", plan_json]
    )
    ctx = _ctx(provider, {"goal": "ship the feature"}, agent_type="planner_agent")

    result = await PlannerAgent().run(ctx)

    assert result.output["plan"] == [
        {"step": "collect data", "rationale": "need inputs", "depends_on": []}
    ]
    assert result.output["analysis"].startswith("- subproblem")
    assert len(ctx.charges) == 4  # analyze + draft + critique + finalize


async def test_executor_agent_answers_directly_without_tools() -> None:
    provider = ScriptedLLMProvider(
        ['{"action": "final", "tool": null, "args": null, "answer": "the answer is 42"}']
    )
    ctx = _ctx(provider, {"instruction": "what is the answer"}, agent_type="executor_agent")

    result = await ExecutorAgent().run(ctx)

    assert result.output["result"] == "the answer is 42"
    assert result.output["tool_calls"] == []
    assert len(ctx.charges) == 1  # only the decide call


async def test_executor_agent_calls_a_tool_then_answers() -> None:
    seen: list[dict[str, Any]] = []

    async def echo(_ctx: StepContext, args: dict[str, Any]) -> Any:
        seen.append(args)
        return {"echoed": args}

    tools = ToolRegistry()
    tools.register(FunctionTool("echo", "echo the args back", echo))

    provider = ScriptedLLMProvider(
        [
            '{"action": "tool", "tool": "echo", "args": {"x": 1}, "answer": null}',
            '{"action": "final", "tool": null, "args": null, "answer": "done"}',
        ]
    )
    ctx = _ctx(provider, {"instruction": "use the echo tool"}, agent_type="executor_agent")

    result = await ExecutorAgent(tools).run(ctx)

    assert seen == [{"x": 1}]
    assert result.output["result"] == "done"
    assert result.output["tool_calls"][0]["tool"] == "echo"
    assert result.output["tool_calls"][0]["result"] == {"echoed": {"x": 1}}


async def test_executor_agent_stops_after_the_tool_call_cap() -> None:
    async def loop_tool(_ctx: StepContext, _args: dict[str, Any]) -> Any:
        return "again"

    tools = ToolRegistry()
    tools.register(FunctionTool("loop", "never satisfied", loop_tool))

    call_tool = '{"action": "tool", "tool": "loop", "args": {}, "answer": null}'
    # The model never volunteers a final answer; the agent must break the loop
    # itself once the iteration cap is hit and then synthesise a response.
    provider = ScriptedLLMProvider([call_tool] * 5 + ["final answer after hitting the cap"])
    ctx = _ctx(provider, {"instruction": "spin"}, agent_type="executor_agent")

    result = await ExecutorAgent(tools).run(ctx)

    assert len(result.output["tool_calls"]) < _MAX_TOOL_CALLS + 1
    assert result.output["tool_calls"]  # it did call the tool
    assert result.output["result"] == "final answer after hitting the cap"


async def test_validator_agent_passes_a_clean_output() -> None:
    provider = ScriptedLLMProvider(
        [
            '{"structure_ok": true, "structure_issues": []}',
            '{"content_ok": true, "content_issues": []}',
        ]
    )
    ctx = _ctx(provider, {"output": {"name": "acme"}}, agent_type="validator_agent")

    result = await ValidatorAgent().run(ctx)

    assert result.output["valid"] is True
    assert result.output["issues"] == []
    assert result.output["score"] == 1.0


async def test_validator_agent_fails_and_scores_down_on_issues() -> None:
    provider = ScriptedLLMProvider(
        [
            '{"structure_ok": false, "structure_issues": ["missing id"]}',
            '{"content_ok": false, "content_issues": ["wrong total", "stale date"]}',
        ]
    )
    ctx = _ctx(provider, {"output": {}}, agent_type="validator_agent")

    result = await ValidatorAgent().run(ctx)

    assert result.output["valid"] is False
    assert result.output["issues"] == ["missing id", "wrong total", "stale date"]
    assert result.output["score"] == pytest.approx(0.25)  # 1.0 - 0.25 * 3


async def test_reflector_agent_revises_the_output() -> None:
    provider = ScriptedLLMProvider(
        [
            "root cause: the total ignored tax",
            '{"corrected_output": {"total": 110}, "changes": ["added 10% tax"]}',
        ]
    )
    ctx = _ctx(
        provider,
        {"output": {"total": 100}, "issues": ["wrong total"], "task": "compute the invoice"},
        agent_type="reflector_agent",
    )

    result = await ReflectorAgent().run(ctx)

    assert result.output["corrected_output"] == {"total": 110}
    assert result.output["changes"] == ["added 10% tax"]
    assert result.output["diagnosis"].startswith("root cause")


async def test_ask_json_repairs_a_malformed_response() -> None:
    provider = ScriptedLLMProvider(
        [
            "- one\n- two",  # analyze
            "here you go: {step: bad}",  # draft — not valid JSON
            '[{"step": "x", "rationale": "y", "depends_on": []}]',  # repair round
            "fine",  # critique
            '[{"step": "x", "rationale": "y", "depends_on": []}]',  # finalize
        ]
    )
    ctx = _ctx(provider, {"goal": "g"}, agent_type="planner_agent")

    result = await PlannerAgent().run(ctx)

    assert result.output["plan"][0]["step"] == "x"
    assert len(ctx.charges) == 5  # the extra repair call


async def test_register_base_agents_wires_the_four_agents() -> None:
    reg = StepRegistry()
    register_base_agents(reg)

    for name in ("planner_agent", "executor_agent", "validator_agent", "reflector_agent"):
        assert name in reg


async def test_planner_agent_runs_as_a_workflow_step() -> None:
    plan_json = '[{"step": "do it", "rationale": "because", "depends_on": []}]'
    provider = ScriptedLLMProvider(["- sub", plan_json, "ok", plan_json])

    journal = InMemoryJournal()
    leases = InMemoryLeaseStore(journal)
    defs = InMemoryDefinitions()
    models = ModelRegistry.from_dict(yaml.safe_load(_YAML))
    providers = LLMProviderRegistry()
    providers.register(provider)
    llm = LLMClient(CostAwareRouter(models), models, providers)

    registry = StepRegistry()
    register_base_agents(registry)

    driver = WorkflowDriver(
        journal,
        defs,
        StepExecutor(registry),
        FakeDeadLetters(),  # type: ignore[arg-type]
        llm=llm,
        budget=BudgetService(InMemoryBudgetLedger(), tenant_daily_limit_usd=100.0),
        clock=FixedClock(T0),
        ids=SequentialIdGenerator("ev"),
    )

    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        steps=(make_step("plan", agent_type="planner_agent"),),
    )
    defs.add(wf)
    await seed_instance(journal, wf, context={"goal": "ship"}, budget_limit_usd=100.0)

    lease = (await leases.acquire_runnable("w1", 5, T0))[0]
    report = await driver.drive(lease, leases.make_guard(lease))
    await leases.release("w1", lease.instance_id)

    assert report.result is DriveResult.COMPLETED
    inst = await journal.get_instance("inst-1", TENANT, definition=wf)
    assert inst.step_states["plan"].output["plan"][0]["step"] == "do it"
