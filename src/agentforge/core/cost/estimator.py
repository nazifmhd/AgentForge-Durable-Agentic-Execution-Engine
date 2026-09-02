"""Token estimation for pre-flight cost projection.

An LLM provider that exposes ``count_tokens`` gives an exact figure; when it
doesn't (or the call would cost a round trip we don't want), the heuristic here
is close enough to gate a budget decision. ~4 chars/token is the well-worn
approximation for English; we round up and add per-message overhead.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

_CHARS_PER_TOKEN = 4
_PER_MESSAGE_OVERHEAD = 4


def estimate_text_tokens(text: str) -> int:
    return -(-len(text) // _CHARS_PER_TOKEN)  # ceil division


def estimate_message_tokens(
    messages: Sequence[dict[str, Any]],
    *,
    system: str | None = None,
    tools: Sequence[dict[str, Any]] | None = None,
) -> int:
    total = estimate_text_tokens(system) if system else 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        else:  # list of blocks
            for block in content:
                total += estimate_text_tokens(str(block.get("text", block)))
        total += _PER_MESSAGE_OVERHEAD
    if tools:
        for tool in tools:
            total += estimate_text_tokens(str(tool))
    return total
