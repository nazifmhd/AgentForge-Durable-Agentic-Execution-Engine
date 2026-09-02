"""Concrete action providers.

* :class:`NoopActionProvider` — records the call, does nothing external (default
  for dry runs / local dev).
* :class:`HttpActionProvider` — POSTs the effect to a configured URL, passing the
  idempotency key as a header. The native workhorse until the n8n adapter lands
  in Phase 9.
"""

from __future__ import annotations

from typing import Any

import httpx

from agentforge.integrations.actions.base import (
    BaseActionProvider,
    EffectRequest,
    EffectResult,
)
from agentforge.logging import get_logger

log = get_logger("actions")


class NoopActionProvider(BaseActionProvider):
    name = "noop"

    def __init__(self) -> None:
        self.calls: list[EffectRequest] = []

    def supports_idempotency_key(self, effect_name: str) -> bool:
        return True  # trivially exactly-once: it does nothing

    async def execute(self, req: EffectRequest) -> EffectResult:
        self.calls.append(req)
        log.info("noop_effect", effect=req.effect_name, step_id=req.step_id, params=req.params)
        return EffectResult(ok=True, data={"noop": True}, provider_ref=req.idempotency_key)


class HttpActionProvider(BaseActionProvider):
    name = "http"

    def __init__(
        self,
        *,
        endpoints: dict[str, str],
        idempotent_effects: set[str] | None = None,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoints = endpoints
        self._idempotent = idempotent_effects or set()
        self._timeout = timeout_seconds
        self._client = client

    def supports_idempotency_key(self, effect_name: str) -> bool:
        return effect_name in self._idempotent

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def execute(self, req: EffectRequest) -> EffectResult:
        url = self._endpoints.get(req.effect_name)
        if url is None:
            return EffectResult(ok=False, error=f"no endpoint configured for {req.effect_name!r}")
        client = await self._http()
        resp = await client.post(
            url,
            json={"effect": req.effect_name, "params": req.params},
            headers={"Idempotency-Key": req.idempotency_key},
        )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json() if resp.content else {}
        return EffectResult(ok=True, data=body, provider_ref=body.get("id") or req.idempotency_key)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
