"""OpenAI provider (Chat Completions). The model id comes from the cost router."""

from __future__ import annotations

import json
from typing import Any

from agentforge.exceptions import (
    ConfigurationError,
    LLMError,
    LLMTimeoutError,
    MalformedOutputError,
    RateLimitError,
)
from agentforge.integrations.llm.base import LLMRequest, LLMResponse, ToolCall

_SERVER_ERROR = 500


def _load_sdk() -> Any:
    try:
        import openai
    except ImportError as exc:  # pragma: no cover - depends on the `agents` extra
        raise ConfigurationError(
            "the `openai` package is required for OpenAIProvider (install the 'agents' extra)"
        ) from exc
    return openai


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, api_key: str | None = None, client: Any | None = None) -> None:
        self._sdk = _load_sdk()
        self._client = client or self._sdk.AsyncOpenAI(api_key=api_key)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        messages: list[dict[str, Any]] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.extend(req.as_dicts())

        kwargs: dict[str, Any] = {
            "model": req.model_id,
            "messages": messages,
            "max_tokens": req.max_tokens,
        }
        if req.tools:
            kwargs["tools"] = req.tools
        if req.stop:
            kwargs["stop"] = req.stop

        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except self._sdk.RateLimitError as exc:
            raise RateLimitError(str(exc)) from exc
        except (self._sdk.APITimeoutError, self._sdk.APIConnectionError) as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except self._sdk.APIStatusError as exc:
            if getattr(exc, "status_code", _SERVER_ERROR) >= _SERVER_ERROR:
                raise LLMTimeoutError(str(exc)) from exc
            raise LLMError(str(exc)) from exc

        choice = resp.choices[0]
        tool_calls: list[ToolCall] = []
        for tc in choice.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        text = choice.message.content or ""
        if not text and not tool_calls:
            raise MalformedOutputError("empty completion from OpenAI")

        usage = resp.usage
        return LLMResponse(
            model_id=req.model_id,
            text=text,
            tool_calls=tool_calls,
            tokens_input=usage.prompt_tokens if usage else 0,
            tokens_output=usage.completion_tokens if usage else 0,
            stop_reason=choice.finish_reason,
            raw={"id": resp.id},
        )

    async def count_tokens(self, req: LLMRequest) -> int | None:
        return None  # OpenAI has no token-count endpoint; heuristic estimator is used
