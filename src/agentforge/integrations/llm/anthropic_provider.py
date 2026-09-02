"""Anthropic provider.

Non-streaming ``messages.create``; ``messages.count_tokens`` powers exact
pre-flight estimation. Thinking config is left unset so each model applies its
own default (adaptive on Sonnet 5 / Opus 5, off on Haiku 4.5). The model id is
supplied by the cost router, not hardcoded here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentforge.exceptions import (
    ConfigurationError,
    LLMError,
    LLMTimeoutError,
    MalformedOutputError,
    RateLimitError,
)
from agentforge.integrations.llm.base import LLMRequest, LLMResponse, ToolCall

if TYPE_CHECKING:
    pass

_SERVER_ERROR = 500


def _load_sdk() -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the `agents` extra
        raise ConfigurationError(
            "the `anthropic` package is required for AnthropicProvider (install the 'agents' extra)"
        ) from exc
    return anthropic


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None, client: Any | None = None) -> None:
        self._sdk = _load_sdk()
        self._client = client or self._sdk.AsyncAnthropic(api_key=api_key)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": req.model_id,
            "max_tokens": req.max_tokens,
            "messages": req.as_dicts(),
        }
        if req.system:
            kwargs["system"] = req.system
        if req.tools:
            kwargs["tools"] = req.tools
        if req.stop:
            kwargs["stop_sequences"] = req.stop

        try:
            msg = await self._client.messages.create(**kwargs)
        except self._sdk.RateLimitError as exc:
            raise RateLimitError(str(exc)) from exc
        except (self._sdk.APITimeoutError, self._sdk.APIConnectionError) as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except self._sdk.APIStatusError as exc:
            if exc.status_code >= _SERVER_ERROR:
                raise LLMTimeoutError(str(exc)) from exc
            raise LLMError(str(exc)) from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in msg.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )

        if not text_parts and not tool_calls:
            raise MalformedOutputError("empty completion from Anthropic")

        return LLMResponse(
            model_id=req.model_id,
            text="".join(text_parts),
            tool_calls=tool_calls,
            tokens_input=msg.usage.input_tokens,
            tokens_output=msg.usage.output_tokens,
            stop_reason=msg.stop_reason,
            raw={"id": msg.id},
        )

    async def count_tokens(self, req: LLMRequest) -> int | None:
        kwargs: dict[str, Any] = {
            "model": req.model_id,
            "messages": req.as_dicts(),
        }
        if req.system:
            kwargs["system"] = req.system
        if req.tools:
            kwargs["tools"] = req.tools
        try:
            result = await self._client.messages.count_tokens(**kwargs)
        except Exception:  # noqa: BLE001 - fall back to the heuristic estimator
            return None
        return int(result.input_tokens)
