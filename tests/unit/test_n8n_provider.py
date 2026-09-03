"""Phase 9 — the n8n ActionProvider adapter."""

from __future__ import annotations

import httpx
import orjson
import pytest

from agentforge.integrations.actions import N8nActionProvider, build_n8n_provider
from agentforge.integrations.actions.base import EffectRequest, EffectResult

_BASE = "https://n8n.example"


def _req(effect: str = "sales.send_email", *, key: str = "idem-1") -> EffectRequest:
    return EffectRequest(
        effect_name=effect,
        params={"to": "x@example.com"},
        idempotency_key=key,
        instance_id="inst-1",
        tenant_id="tenant-1",
        step_id="send",
    )


@pytest.fixture
def respx_mock():
    import respx

    with respx.mock(assert_all_called=False) as mock:
        yield mock


async def test_execute_posts_to_the_effect_webhook(respx_mock) -> None:
    route = respx_mock.post(f"{_BASE}/webhook/sales.send_email").mock(
        return_value=httpx.Response(200, json={"id": "msg-42"})
    )
    provider = N8nActionProvider(base_url=_BASE, api_key="secret")

    result = await provider.execute(_req())

    assert isinstance(result, EffectResult)
    assert result.ok and result.provider_ref == "msg-42"
    request = route.calls[0].request
    assert request.headers["Idempotency-Key"] == "idem-1"
    assert request.headers["X-N8N-API-KEY"] == "secret"
    assert request.headers["X-AgentForge-Tenant"] == "tenant-1"
    body = orjson.loads(request.content)
    assert body["effect"] == "sales.send_email"
    assert body["params"] == {"to": "x@example.com"}
    assert body["step_id"] == "send"
    await provider.aclose()


async def test_execute_raises_on_http_error(respx_mock) -> None:
    respx_mock.post(f"{_BASE}/webhook/boom").mock(return_value=httpx.Response(500))
    provider = N8nActionProvider(base_url=_BASE)

    with pytest.raises(httpx.HTTPStatusError):
        await provider.execute(_req("boom"))


def test_supports_idempotency_key_only_for_declared_effects() -> None:
    provider = N8nActionProvider(base_url=_BASE, idempotent_effects={"sales.send_email"})
    assert provider.supports_idempotency_key("sales.send_email") is True
    assert provider.supports_idempotency_key("sales.create_crm_task") is False


async def test_reconcile_returns_none_unless_effect_is_reconcilable(respx_mock) -> None:
    provider = N8nActionProvider(base_url=_BASE, reconcile_effects={"sales.send_email"})

    assert await provider.reconcile(_req("other")) is None

    respx_mock.get(f"{_BASE}/webhook/sales.send_email/status").mock(
        return_value=httpx.Response(200, json={"found": True, "id": "msg-9"})
    )
    hit = await provider.reconcile(_req())
    assert hit is not None and hit.provider_ref == "msg-9"


async def test_reconcile_returns_none_when_n8n_has_no_record(respx_mock) -> None:
    provider = N8nActionProvider(base_url=_BASE, reconcile_effects={"sales.send_email"})
    respx_mock.get(f"{_BASE}/webhook/sales.send_email/status").mock(
        return_value=httpx.Response(200, json={"found": False})
    )
    assert await provider.reconcile(_req()) is None


async def test_compensate_posts_only_for_compensable_effects(respx_mock) -> None:
    provider = N8nActionProvider(base_url=_BASE, compensate_effects={"sales.send_email"})
    executed = EffectResult(ok=True, data={"id": "msg-1"}, provider_ref="msg-1")

    assert await provider.compensate(_req("other"), executed) is None

    route = respx_mock.post(f"{_BASE}/webhook/sales.send_email/compensate").mock(
        return_value=httpx.Response(200, json={"recalled": True})
    )
    undone = await provider.compensate(_req(), executed)
    assert undone is not None and undone.data == {"recalled": True}
    body = orjson.loads(route.calls[0].request.content)
    assert body["executed"] == {"id": "msg-1"}
    assert body["ref"] == "msg-1"


def test_build_n8n_provider_wires_reconcile_from_idempotent_set() -> None:
    provider = build_n8n_provider(base_url=_BASE + "/", api_key="k", idempotent_effects={"a", "b"})
    assert provider.name == "n8n"
    assert provider.supports_idempotency_key("a")
    assert provider._reconcilable == {"a", "b"}  # type: ignore[attr-defined]
