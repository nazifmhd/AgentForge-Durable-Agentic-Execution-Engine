"""An ``ActionProvider`` for the Sales Intelligence workflow's two side effects:
``sales.create_crm_task`` and ``sales.send_email``.

The in-memory implementation is what the reference workflow and its tests run
against — it is idempotency-key aware (exactly-once) and supports compensation
(cancel the task, recall/void the email). A real deployment swaps this for one
that talks to a CRM and an ESP; the effect names and payload shape stay the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentforge.integrations.actions.base import BaseActionProvider, EffectRequest, EffectResult

CREATE_CRM_TASK = "sales.create_crm_task"
SEND_EMAIL = "sales.send_email"
_EFFECTS = (CREATE_CRM_TASK, SEND_EMAIL)


@dataclass
class _Record:
    ref: str
    effect_name: str
    params: dict[str, Any]
    status: str = "done"  # done | cancelled | recalled


class InMemorySalesProvider(BaseActionProvider):
    name = "sales"

    def __init__(self) -> None:
        self._by_key: dict[str, _Record] = {}
        self.crm_tasks: list[_Record] = []
        self.emails: list[_Record] = []
        self._seq = 0

    def supports_idempotency_key(self, effect_name: str) -> bool:
        return effect_name in _EFFECTS

    async def execute(self, req: EffectRequest) -> EffectResult:
        existing = self._by_key.get(req.idempotency_key)
        if existing is not None:  # provider-side dedup
            return EffectResult(ok=True, data={"ref": existing.ref}, provider_ref=existing.ref)

        self._seq += 1
        if req.effect_name == CREATE_CRM_TASK:
            rec = _Record(f"crm-task-{self._seq}", req.effect_name, req.params)
            self.crm_tasks.append(rec)
        elif req.effect_name == SEND_EMAIL:
            rec = _Record(f"email-{self._seq}", req.effect_name, req.params)
            self.emails.append(rec)
        else:  # pragma: no cover - defensive
            return EffectResult(ok=False, error=f"unknown effect {req.effect_name}")

        self._by_key[req.idempotency_key] = rec
        return EffectResult(ok=True, data={"ref": rec.ref}, provider_ref=rec.ref)

    async def reconcile(self, req: EffectRequest) -> EffectResult | None:
        rec = self._by_key.get(req.idempotency_key)
        if rec is None:
            return None
        return EffectResult(ok=True, data={"ref": rec.ref}, provider_ref=rec.ref)

    async def compensate(self, req: EffectRequest, executed: EffectResult) -> EffectResult | None:
        rec = self._by_key.get(req.idempotency_key)
        if rec is None:
            return None
        rec.status = "cancelled" if req.effect_name == CREATE_CRM_TASK else "recalled"
        return EffectResult(ok=True, data={"ref": rec.ref, "status": rec.status})


@dataclass
class WebhookSalesProvider(BaseActionProvider):
    """Posts each effect to an outbound webhook (e.g. an n8n / Zapier flow that
    owns the CRM + ESP integration). Idempotency is delegated to the receiver via
    the ``Idempotency-Key`` header."""

    webhook_url: str
    name: str = "sales"
    _client: Any | None = field(default=None, repr=False)

    def supports_idempotency_key(self, effect_name: str) -> bool:
        return effect_name in _EFFECTS

    async def _http(self) -> Any:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def execute(self, req: EffectRequest) -> EffectResult:
        client = await self._http()
        resp = await client.post(
            self.webhook_url,
            json={"effect": req.effect_name, "params": req.params},
            headers={"Idempotency-Key": req.idempotency_key},
        )
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        return EffectResult(ok=True, data=body, provider_ref=body.get("ref"))
