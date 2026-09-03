"""Run an :class:`EvalSuite` against a registered agent and score every case."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentforge.core.llm_client import LLMClient
from agentforge.core.ports import SYSTEM_CLOCK, Clock
from agentforge.core.runners import StepContext, StepRegistry
from agentforge.evals.models import CaseResult, EvalCase, EvalSuite, ScoreResult, SuiteReport
from agentforge.evals.scorers import ScorerDeps, get_scorer
from agentforge.logging import get_logger

log = get_logger("evals")


def load_suite(path: str | Path) -> EvalSuite:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"eval suite not found: {p}")
    return EvalSuite.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))


class EvalRunner:
    def __init__(
        self,
        registry: StepRegistry,
        llm: LLMClient,
        *,
        judge: LLMClient | None = None,
        judge_tier: str = "standard",
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._registry = registry
        self._llm = llm
        self._deps = ScorerDeps(judge=judge or llm, judge_tier=judge_tier)
        self._clock = clock

    async def run_suite(self, suite: EvalSuite) -> SuiteReport:
        runner = self._registry.get(suite.target)  # fail fast on a bad target
        results: list[CaseResult] = []
        for case in suite.cases:
            results.append(await self._run_case(suite, case, runner))

        pass_rate = sum(c.weighted_score for c in results) / len(results) if results else 0.0
        return SuiteReport(
            suite=suite.name,
            target=suite.target,
            threshold=suite.threshold,
            passed=pass_rate >= suite.threshold,
            pass_rate=pass_rate,
            case_count=len(results),
            cases=results,
            total_cost_usd=sum(c.cost_usd for c in results),
            total_tokens_input=sum(c.tokens_input for c in results),
            total_tokens_output=sum(c.tokens_output for c in results),
        )

    async def _run_case(self, suite: EvalSuite, case: EvalCase, runner: Any) -> CaseResult:
        ctx = StepContext(
            instance_id=f"eval-{suite.name}",
            tenant_id="eval",
            step_id=case.name,
            agent_type=suite.target,
            attempt=1,
            inputs=dict(case.inputs),
            instance_context={},
            clock=self._clock,
            llm_client=self._llm,
        )
        started = self._clock.now()
        try:
            result = await runner.run(ctx)
            output = result.output
        except Exception as exc:  # noqa: BLE001 - a crashing agent is a failed case
            log.warning("eval_case_error", case=case.name, error=str(exc))
            return CaseResult(
                name=case.name,
                passed=False,
                weighted_score=0.0,
                scores=[],
                cost_usd=sum(c.amount_usd for c in ctx.charges),
                error=f"{type(exc).__name__}: {exc}",
                latency_seconds=(self._clock.now() - started).total_seconds(),
            )

        elapsed = (self._clock.now() - started).total_seconds()
        scores = [await self._score(check, output) for check in case.checks]
        total_weight = sum(s.weight for s in scores) or 1.0
        weighted = sum(s.score * s.weight for s in scores) / total_weight
        return CaseResult(
            name=case.name,
            passed=all(s.passed for s in scores),
            weighted_score=weighted,
            scores=scores,
            output=output,
            cost_usd=sum(c.amount_usd for c in ctx.charges),
            tokens_input=sum(c.tokens_input for c in ctx.charges),
            tokens_output=sum(c.tokens_output for c in ctx.charges),
            latency_seconds=elapsed,
        )

    async def _score(self, check: Any, output: dict[str, Any]) -> ScoreResult:
        scorer = get_scorer(check.scorer)
        result = await scorer(output, check.args, self._deps)
        result.weight = check.weight
        return result
