"""Small helpers for prompting through ``StepContext.llm`` (cost-aware, budgeted)."""

from __future__ import annotations

from typing import Any

import orjson

from agentforge.core.domain.enums import CostTier
from agentforge.core.runners import StepContext
from agentforge.exceptions import MalformedOutputError
from agentforge.integrations.llm.base import LLMMessage


async def ask_text(
    ctx: StepContext,
    *,
    system: str,
    user: str,
    tier: CostTier | str = "standard",
    max_tokens: int = 2048,
    task_type: str = "general",
) -> str:
    resp = await ctx.llm(
        [LLMMessage(role="user", content=user)],
        system=system,
        tier=tier,
        max_tokens=max_tokens,
        task_type=task_type,
    )
    return resp.text.strip()


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("`").strip()
    return orjson.loads(text)


async def ask_json(
    ctx: StepContext,
    *,
    system: str,
    user: str,
    tier: CostTier | str = "standard",
    max_tokens: int = 2048,
    task_type: str = "general",
) -> Any:
    """Prompt for JSON, tolerating code fences, with one repair round on failure."""
    json_system = f"{system}\n\nRespond with a single valid JSON value and nothing else."
    text = await ask_text(
        ctx, system=json_system, user=user, tier=tier, max_tokens=max_tokens, task_type=task_type
    )
    try:
        return _extract_json(text)
    except (orjson.JSONDecodeError, ValueError, IndexError) as first:
        repair = await ask_text(
            ctx,
            system="You fix malformed JSON. Output only the corrected JSON value.",
            user=f"This was supposed to be JSON but did not parse ({first}):\n\n{text}",
            tier=tier,
            max_tokens=max_tokens,
            task_type="json_repair",
        )
        try:
            return _extract_json(repair)
        except (orjson.JSONDecodeError, ValueError, IndexError) as second:
            raise MalformedOutputError(f"model did not return parseable JSON: {second}") from second
