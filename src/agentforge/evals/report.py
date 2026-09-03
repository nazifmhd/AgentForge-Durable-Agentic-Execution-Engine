"""Render a :class:`SuiteReport` as a console table or a JSON file."""

from __future__ import annotations

from pathlib import Path

import orjson

from agentforge.evals.models import SuiteReport


def render_text(report: SuiteReport) -> str:
    lines: list[str] = []
    verdict = "PASS" if report.passed else "FAIL"
    lines.append(f"{verdict}  {report.suite}  (target: {report.target})")
    lines.append(
        f"  pass rate {report.pass_rate:.0%}  threshold {report.threshold:.0%}  "
        f"cases {report.case_count}"
    )
    lines.append(
        f"  cost ${report.total_cost_usd:.4f}  "
        f"tokens {report.total_tokens_input}in / {report.total_tokens_output}out"
    )
    lines.append("")
    for case in report.cases:
        mark = "ok  " if case.passed else "FAIL"
        lines.append(f"  {mark} {case.name}  ({case.weighted_score:.0%})")
        if case.error:
            lines.append(f"        error: {case.error}")
        for score in case.scores:
            smark = "·" if score.passed else "✗"
            lines.append(f"        {smark} {score.scorer}: {score.detail}")
    return "\n".join(lines)


def to_json(report: SuiteReport) -> bytes:
    return orjson.dumps(report.model_dump(), option=orjson.OPT_INDENT_2)


def write_json(report: SuiteReport, path: str | Path) -> None:
    Path(path).write_bytes(to_json(report))
