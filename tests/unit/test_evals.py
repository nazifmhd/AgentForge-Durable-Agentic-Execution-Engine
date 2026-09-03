"""Phase 10 — the offline eval harness."""

from __future__ import annotations

import yaml
from tests.doubles import ScriptedLLMProvider

from agentforge.core.cost.registry import ModelRegistry
from agentforge.core.cost.router import CostAwareRouter
from agentforge.core.llm_client import LLMClient
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.evals import (
    EvalRunner,
    EvalSuite,
    load_suite,
    render_text,
    resolve_path,
    to_json,
)
from agentforge.evals.scorers import ScorerDeps, get_scorer
from agentforge.integrations.llm.base import LLMProviderRegistry

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


def _llm(*script: str) -> LLMClient:
    models = ModelRegistry.from_dict(yaml.safe_load(_YAML))
    providers = LLMProviderRegistry()
    providers.register(ScriptedLLMProvider(list(script) or ["7"]))
    return LLMClient(CostAwareRouter(models), models, providers)


# --- path + scorers ------------------------------------------------


def test_resolve_path_walks_dicts_and_lists() -> None:
    root = {"score": {"tier": "hot"}, "plan": [{"step": "a"}, {"step": "b"}]}
    assert resolve_path(root, "score.tier") == "hot"
    assert resolve_path(root, "plan.1.step") == "b"
    assert resolve_path(root, "plan.9") is not None  # missing -> sentinel, not crash
    assert resolve_path(root, "") == root


async def test_scorers_pass_and_fail() -> None:
    deps = ScorerDeps(judge=None)
    out = {"tier": "hot", "fit_score": 82, "rationale": "good", "meta": {"a": 1}}

    async def run(name: str, args: dict[str, object]) -> bool:
        return (await get_scorer(name)(out, args, deps)).passed

    assert await run("equals", {"path": "tier", "value": "hot"})
    assert not await run("equals", {"path": "tier", "value": "cold"})
    assert await run("one_of", {"path": "tier", "options": ["hot", "warm"]})
    assert await run("in_range", {"path": "fit_score", "min": 50})
    assert not await run("in_range", {"path": "fit_score", "max": 50})
    assert await run("non_empty", {"path": "rationale"})
    assert not await run("non_empty", {"path": "missing"})
    assert await run("json_keys", {"path": "meta", "required": ["a"]})
    assert await run("contains", {"path": "rationale", "substring": "GOOD"})


async def test_llm_judge_uses_the_judge_model() -> None:
    deps = ScorerDeps(judge=_llm("8"))
    result = await get_scorer("llm_judge")(
        {"text": "a plan"}, {"rubric": "is it a plan?", "threshold": 0.7}, deps
    )
    assert result.passed and result.score == 0.8


async def test_llm_judge_without_a_judge_fails_soft() -> None:
    result = await get_scorer("llm_judge")({}, {"rubric": "x"}, ScorerDeps(judge=None))
    assert not result.passed and "no judge" in result.detail


# --- suite loading + running -------------------------------------


def test_load_suite_from_yaml(tmp_path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "s",
                "target": "echo_agent",
                "cases": [{"name": "c1", "inputs": {"x": 1}, "checks": [{"scorer": "non_empty"}]}],
            }
        )
    )
    suite = load_suite(path)
    assert suite.target == "echo_agent"
    assert suite.cases[0].checks[0].scorer == "non_empty"


def _suite(**over: object) -> EvalSuite:
    base = {
        "name": "demo",
        "target": "demo_agent",
        "threshold": 0.75,
        "cases": [
            {
                "name": "good",
                "inputs": {"topic": "durability"},
                "checks": [
                    {"scorer": "equals", "args": {"path": "verdict", "value": "ok"}},
                    {"scorer": "non_empty", "args": {"path": "notes"}},
                ],
            },
            {
                "name": "bad",
                "inputs": {"topic": "boom"},
                "checks": [{"scorer": "equals", "args": {"path": "verdict", "value": "ok"}}],
            },
        ],
    }
    base.update(over)
    return EvalSuite.model_validate(base)


def _registry() -> StepRegistry:
    reg = StepRegistry()

    async def demo(ctx: StepContext) -> StepResult:
        if ctx.inputs.get("topic") == "boom":
            raise RuntimeError("agent exploded")
        return StepResult(output={"verdict": "ok", "notes": f"about {ctx.inputs['topic']}"})

    reg.register("demo_agent", FunctionRunner(demo))
    return reg


async def test_run_suite_scores_cases_and_rolls_up() -> None:
    report = await EvalRunner(_registry(), _llm()).run_suite(_suite())

    assert report.case_count == 2
    good, bad = report.cases
    assert good.passed and good.weighted_score == 1.0
    assert not bad.passed
    assert bad.error is not None and "agent exploded" in bad.error
    assert report.pass_rate == 0.5
    assert report.passed is False  # 0.5 < 0.75


async def test_threshold_override_flips_the_verdict() -> None:
    suite = _suite(threshold=0.4)
    report = await EvalRunner(_registry(), _llm()).run_suite(suite)
    assert report.pass_rate == 0.5
    assert report.passed is True  # 0.5 >= 0.4


async def test_render_and_json_are_produced() -> None:
    report = await EvalRunner(_registry(), _llm()).run_suite(_suite())
    text = render_text(report)
    assert "FAIL" in text and "good" in text and "bad" in text
    blob = to_json(report)
    assert b'"suite"' in blob and b'"pass_rate"' in blob


def test_shipped_suites_are_valid() -> None:
    for name in ("planner_agent", "sales_scoring"):
        suite = load_suite(f"evals/suites/{name}.yaml")
        assert suite.cases
        for case in suite.cases:
            for check in case.checks:
                get_scorer(check.scorer)  # raises if unknown
