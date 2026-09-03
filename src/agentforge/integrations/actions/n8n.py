"""n8n as an :class:`ActionProvider` (ADR-0007).

Each effect maps to an n8n **webhook workflow**: AgentForge POSTs the effect
payload to ``{base_url}{webhook_prefix}/{effect_name}`` and n8n does the outside-
world work (send the email, write the CRM row, ping Slack…). The n8n workflow is
expected to:

- treat the ``Idempotency-Key`` header as a dedupe key when the effect is marked
  idempotent here (so a retried step does not double-fire);
- return a small JSON body, ideally ``{"id": "<external ref>"}``.

Optional companion webhooks give ``reconcile`` (did a prior attempt land?) and
``compensate`` (undo) their own n8n workflows.
"""

from __future__ import annotations

from typing import Any

import httpx

from agentforge.integrations.actions.base import BaseActionProvider, EffectRequest, EffectResult
from agentforge.logging import get_logger

log = get_logger("n8n")


class N8nActionProvider(BaseActionProvider):
    name = "n8n"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        webhook_prefix: str = "/webhook",
        idempotent_effects: set[str] | None = None,
        reconcile_effects: set[str] | None = None,
        compensate_effects: set[str] | None = None,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._prefix = "/" + webhook_prefix.strip("/")
        self._idempotent = idempotent_effects or set()
        self._reconcilable = reconcile_effects or set()
        self._compensable = compensate_effects or set()
        self._timeout = timeout_seconds
        self._client = client

    def supports_idempotency_key(self, effect_name: str) -> bool:
        return effect_name in self._idempotent

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _url(self, effect_name: str, *, suffix: str = "") -> str:
        return f"{self._base_url}{self._prefix}/{effect_name}{suffix}"

    def _headers(self, req: EffectRequest) -> dict[str, str]:
        headers = {
            "Idempotency-Key": req.idempotency_key,
            "X-AgentForge-Instance": req.instance_id,
            "X-AgentForge-Tenant": req.tenant_id,
        }
        if self._api_key:
            headers["X-N8N-API-KEY"] = self._api_key
        return headers

    def _payload(self, req: EffectRequest) -> dict[str, Any]:
        return {
            "effect": req.effect_name,
            "params": req.params,
            "instance_id": req.instance_id,
            "tenant_id": req.tenant_id,
            "step_id": req.step_id,
            "idempotency_key": req.idempotency_key,
        }

    async def execute(self, req: EffectRequest) -> EffectResult:
        client = await self._http()
        resp = await client.post(
            self._url(req.effect_name), json=self._payload(req), headers=self._headers(req)
        )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json() if resp.content else {}
        ref = body.get("id") or body.get("ref") or req.idempotency_key
        return EffectResult(ok=True, data=body, provider_ref=str(ref))

    async def reconcile(self, req: EffectRequest) -> EffectResult | None:
        if req.effect_name not in self._reconcilable:
            return None
        client = await self._http()
        try:
            resp = await client.get(
                self._url(req.effect_name, suffix="/status"),
                params={"idempotency_key": req.idempotency_key},
                headers=self._headers(req),
            )
        except httpx.HTTPError:
            return None
        if resp.status_code == httpx.codes.NOT_FOUND:
            return None
        resp.raise_for_status()
        body: dict[str, Any] = resp.json() if resp.content else {}
        if not body.get("found"):
            return None
        return EffectResult(ok=True, data=body, provider_ref=str(body.get("id") or ""))

    async def compensate(self, req: EffectRequest, executed: EffectResult) -> EffectResult | None:
        if req.effect_name not in self._compensable:
            return None
        client = await self._http()
        resp = await client.post(
            self._url(req.effect_name, suffix="/compensate"),
            json={**self._payload(req), "executed": executed.data, "ref": executed.provider_ref},
            headers=self._headers(req),
        )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json() if resp.content else {}
        return EffectResult(ok=True, data=body)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def build_n8n_provider(
    *,
    base_url: str,
    api_key: str | None = None,
    idempotent_effects: set[str] | None = None,
) -> N8nActionProvider:
    return N8nActionProvider(
        base_url=base_url,
        api_key=api_key,
        idempotent_effects=idempotent_effects,
        reconcile_effects=idempotent_effects,  # if it dedupes, it can usually report status
    )
