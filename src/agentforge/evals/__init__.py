"""Offline eval harness for agents.

A suite (YAML) names a target ``agent_type`` and a list of cases; each case has
inputs and named ``checks``. :class:`EvalRunner` runs the agent per case with a
real cost-routed ``LLMClient`` and scores the output. The ``agentforge eval``
CLI renders a report and exits non-zero below the suite threshold — CI-gate
ready.
"""

from __future__ import annotations

from agentforge.evals.models import (
    CaseResult,
    Check,
    EvalCase,
    EvalSuite,
    ScoreResult,
    SuiteReport,
)
from agentforge.evals.report import render_text, to_json, write_json
from agentforge.evals.runner import EvalRunner, load_suite
from agentforge.evals.scorers import SCORERS, ScorerDeps, get_scorer, resolve_path

__all__ = [
    "SCORERS",
    "CaseResult",
    "Check",
    "EvalCase",
    "EvalRunner",
    "EvalSuite",
    "ScoreResult",
    "ScorerDeps",
    "SuiteReport",
    "get_scorer",
    "load_suite",
    "render_text",
    "resolve_path",
    "to_json",
    "write_json",
]
