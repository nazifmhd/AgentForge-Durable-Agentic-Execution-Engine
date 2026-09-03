"""Scorers — named checks a case runs against an agent's output.

A scorer is ``async (value_root, args, deps) -> ScoreResult``. ``args`` come
straight from the suite YAML; ``deps`` carries an optional judge ``LLMClient``.
Most scorers take a ``path`` (dotted, into the output dict — ``""`` / omitted
means the whole output) plus their own comparison args.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agentforge.core.domain.enums import CostTier
from agentforge.core.llm_client import LLMClient
from agentforge.evals.models import ScoreResult
from agentforge.exceptions import ConfigurationError
from agentforge.integrations.llm.base import LLMMessage


@dataclass(slots=True)
class ScorerDeps:
    judge: LLMClient | None = None
    judge_tier: str = "standard"


Scorer = Callable[[Any, dict[str, Any], ScorerDeps], Awaitable[ScoreResult]]

_MISSING = object()


def resolve_path(root: Any, path: str) -> Any:
    if not path:
        return root
    cur = root
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, _MISSING)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else _MISSING
        else:
            return _MISSING
        if cur is _MISSING:
            return _MISSING
    return cur


def _ok(scorer: str, passed: bool, detail: str, *, score: float | None = None) -> ScoreResult:
    return ScoreResult(
        scorer=scorer,
        passed=passed,
        score=(1.0 if passed else 0.0) if score is None else score,
        detail=detail,
    )


async def _equals(root: Any, args: dict[str, Any], _deps: ScorerDeps) -> ScoreResult:
    actual = resolve_path(root, args.get("path", ""))
    expected = args["value"]
    return _ok("equals", actual == expected, f"expected {expected!r}, got {actual!r}")


async def _contains(root: Any, args: dict[str, Any], _deps: ScorerDeps) -> ScoreResult:
    actual = resolve_path(root, args.get("path", ""))
    text = "" if actual is _MISSING else str(actual)
    needles = args.get("substrings") or ([args["substring"]] if "substring" in args else [])
    missing = [n for n in needles if n.lower() not in text.lower()]
    return _ok("contains", not missing, f"missing {missing}" if missing else "all present")


async def _regex(root: Any, args: dict[str, Any], _deps: ScorerDeps) -> ScoreResult:
    actual = resolve_path(root, args.get("path", ""))
    text = "" if actual is _MISSING else str(actual)
    hit = re.search(args["pattern"], text) is not None
    return _ok("regex", hit, f"/{args['pattern']}/ {'matched' if hit else 'did not match'}")


async def _in_range(root: Any, args: dict[str, Any], _deps: ScorerDeps) -> ScoreResult:
    actual = resolve_path(root, args["path"])
    try:
        num = float(actual)
    except (TypeError, ValueError):
        return _ok("in_range", False, f"{actual!r} is not numeric")
    lo, hi = args.get("min", float("-inf")), args.get("max", float("inf"))
    return _ok("in_range", lo <= num <= hi, f"{num} in [{lo}, {hi}]")


async def _one_of(root: Any, args: dict[str, Any], _deps: ScorerDeps) -> ScoreResult:
    actual = resolve_path(root, args.get("path", ""))
    options = args["options"]
    return _ok("one_of", actual in options, f"{actual!r} in {options}")


async def _json_keys(root: Any, args: dict[str, Any], _deps: ScorerDeps) -> ScoreResult:
    actual = resolve_path(root, args.get("path", ""))
    if not isinstance(actual, dict):
        return _ok("json_keys", False, f"not an object: {type(actual).__name__}")
    missing = [k for k in args["required"] if k not in actual]
    detail = f"missing keys {missing}" if missing else "all keys present"
    return _ok("json_keys", not missing, detail)


async def _non_empty(root: Any, args: dict[str, Any], _deps: ScorerDeps) -> ScoreResult:
    actual = resolve_path(root, args.get("path", ""))
    empty = actual is _MISSING or actual in (None, "", [], {}, ())
    return _ok("non_empty", not empty, "empty" if empty else f"len={_safe_len(actual)}")


def _safe_len(v: Any) -> int:
    try:
        return len(v)
    except TypeError:
        return 1


_JUDGE_SYSTEM = (
    "You are a strict evaluation judge. Score how well the OUTPUT satisfies the "
    "RUBRIC on a 0-10 integer scale. Respond with only the number."
)


async def _llm_judge(root: Any, args: dict[str, Any], deps: ScorerDeps) -> ScoreResult:
    if deps.judge is None:
        return ScoreResult(
            scorer="llm_judge", passed=False, score=0.0, detail="no judge LLM configured"
        )
    target = resolve_path(root, args.get("path", ""))
    rubric = args["rubric"]
    pass_at = float(args.get("threshold", 0.7))
    completion = await deps.judge.complete(
        messages=[LLMMessage(role="user", content=f"RUBRIC:\n{rubric}\n\nOUTPUT:\n{target}")],
        system=_JUDGE_SYSTEM,
        tier=_coerce_tier(args.get("tier", deps.judge_tier)),
        task_type="eval_judge",
        max_tokens=8,
    )
    raw = completion.response.text.strip()
    match = re.search(r"\d+(\.\d+)?", raw)
    score01 = min(max(float(match.group()) / 10.0, 0.0), 1.0) if match else 0.0
    return ScoreResult(
        scorer="llm_judge",
        passed=score01 >= pass_at,
        score=score01,
        detail=f"judge said {raw!r} -> {score01:.2f} (need {pass_at:.2f})",
    )


def _coerce_tier(value: Any) -> Any:
    return CostTier(value) if isinstance(value, str) else value


SCORERS: dict[str, Scorer] = {
    "equals": _equals,
    "contains": _contains,
    "regex": _regex,
    "in_range": _in_range,
    "one_of": _one_of,
    "json_keys": _json_keys,
    "non_empty": _non_empty,
    "llm_judge": _llm_judge,
}


def get_scorer(name: str) -> Scorer:
    try:
        return SCORERS[name]
    except KeyError:
        raise ConfigurationError(f"unknown scorer {name!r}; available: {sorted(SCORERS)}") from None
