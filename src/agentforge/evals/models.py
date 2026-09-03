"""Eval data model — suites, cases, and results.

A *suite* is a YAML file: a target agent plus a list of *cases*. Each case has an
input (the ``StepContext.inputs`` dict) and one or more *checks* — named scorer
invocations. Running a suite produces a :class:`SuiteReport` with a pass rate,
per-check breakdown, and cost / latency roll-ups; the CLI exits non-zero when the
pass rate is below the suite's ``threshold``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Check(BaseModel):
    """One scorer invocation against a case's output."""

    model_config = ConfigDict(extra="forbid")

    scorer: str
    # Free-form scorer args, e.g. {"path": "score.tier", "equals": "hot"}.
    args: dict[str, Any] = Field(default_factory=dict)
    weight: float = Field(default=1.0, gt=0)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    checks: list[Check] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    target: str = Field(description="agent_type to resolve from the StepRegistry")
    description: str = ""
    threshold: float = Field(default=0.8, ge=0, le=1, description="min weighted pass rate")
    cases: list[EvalCase] = Field(min_length=1)


class ScoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scorer: str
    passed: bool
    score: float = Field(ge=0, le=1)
    weight: float = 1.0
    detail: str = ""


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    weighted_score: float = Field(ge=0, le=1)
    scores: list[ScoreResult]
    output: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    latency_seconds: float = 0.0
    error: str | None = None


class SuiteReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: str
    target: str
    threshold: float
    passed: bool
    pass_rate: float = Field(ge=0, le=1)
    case_count: int
    cases: list[CaseResult]
    total_cost_usd: float = 0.0
    total_tokens_input: int = 0
    total_tokens_output: int = 0

    @property
    def failed_cases(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.passed]
