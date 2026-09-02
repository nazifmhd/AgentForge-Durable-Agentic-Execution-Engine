from __future__ import annotations

import pytest
from tests.doubles import RecordingActionProvider

from agentforge.core.outbox import InMemoryOutboxStore
from agentforge.core.side_effects import EffectStatus, SideEffectGuard
from agentforge.exceptions import CompensationError, SideEffectError
from agentforge.integrations.actions.base import ProviderRegistry

TENANT = "t1"


def _guard(provider: RecordingActionProvider) -> tuple[SideEffectGuard, InMemoryOutboxStore]:
    reg = ProviderRegistry()
    reg.register(provider)
    store = InMemoryOutboxStore()
    return SideEffectGuard(store, reg), store


async def test_first_call_executes_second_call_dedups() -> None:
    p = RecordingActionProvider(idempotent={"send_email"})
    guard, _ = _guard(p)

    r1 = await guard.execute(
        instance_id="i1",
        tenant_id=TENANT,
        step_id="s1",
        effect_name="send_email",
        params={"to": "a@b.c"},
        provider_name="test",
    )
    r2 = await guard.execute(
        instance_id="i1",
        tenant_id=TENANT,
        step_id="s1",
        effect_name="send_email",
        params={"to": "a@b.c"},
        provider_name="test",
    )
    assert r1.deduplicated is False
    assert r2.deduplicated is True
    assert r1.guarantee == "exactly_once"
    assert len(p.executed) == 1  # provider called once
    assert r2.result.data == r1.result.data


async def test_different_params_are_different_effects() -> None:
    p = RecordingActionProvider()
    guard, _ = _guard(p)
    await guard.execute(
        instance_id="i1",
        tenant_id=TENANT,
        step_id="s1",
        effect_name="post",
        params={"x": 1},
        provider_name="test",
    )
    await guard.execute(
        instance_id="i1",
        tenant_id=TENANT,
        step_id="s1",
        effect_name="post",
        params={"x": 2},
        provider_name="test",
    )
    assert len(p.executed) == 2


async def test_resumed_attempt_reconciles_for_non_idempotent_provider() -> None:
    # simulate: attempt 1 crashed after the outbox row was written (PENDING)
    p = RecordingActionProvider(reconcile_hits={"charge"})
    guard, store = _guard(p)
    key = SideEffectGuard.idempotency_key("i1", "s1", "charge", {"amt": 5})
    from agentforge.core.outbox import OutboxEntry

    await store.claim(
        OutboxEntry(
            idempotency_key=key,
            instance_id="i1",
            tenant_id=TENANT,
            step_id="s1",
            effect_name="charge",
            provider="test",
            params={"amt": 5},
        )
    )  # row now exists, attempts=1

    out = await guard.execute(
        instance_id="i1",
        tenant_id=TENANT,
        step_id="s1",
        effect_name="charge",
        params={"amt": 5},
        provider_name="test",
    )
    assert p.reconciled  # reconcile was consulted
    assert not p.executed  # the prior attempt had landed; we did NOT re-execute
    assert out.result.data == {"reconciled": True}


async def test_provider_failure_marks_outbox_failed_and_raises() -> None:
    p = RecordingActionProvider(fail_effects={"boom"})
    guard, store = _guard(p)
    with pytest.raises(SideEffectError):
        await guard.execute(
            instance_id="i1",
            tenant_id=TENANT,
            step_id="s1",
            effect_name="boom",
            params={},
            provider_name="test",
        )
    rows = await store.list_for_instance("i1", TENANT)
    assert rows[0].status == EffectStatus.FAILED


async def test_compensate_instance_undoes_in_reverse_order() -> None:
    p = RecordingActionProvider()
    guard, _ = _guard(p)
    for name in ("a", "b", "c"):
        await guard.execute(
            instance_id="i1",
            tenant_id=TENANT,
            step_id="s1",
            effect_name=name,
            params={},
            provider_name="test",
        )
    undone = await guard.compensate_instance("i1", TENANT)
    assert [e.effect_name for e in undone] == ["c", "b", "a"]
    assert [r.effect_name for r in p.compensated] == ["c", "b", "a"]

    # a compensated effect can't be re-executed
    with pytest.raises(SideEffectError, match="compensated"):
        await guard.execute(
            instance_id="i1",
            tenant_id=TENANT,
            step_id="s1",
            effect_name="a",
            params={},
            provider_name="test",
        )


async def test_compensation_failure_raises_compensation_error() -> None:
    p = RecordingActionProvider(compensate_fails={"b"})
    guard, _ = _guard(p)
    for name in ("a", "b"):
        await guard.execute(
            instance_id="i1",
            tenant_id=TENANT,
            step_id="s1",
            effect_name=name,
            params={},
            provider_name="test",
        )
    with pytest.raises(CompensationError, match="compensation for b failed"):
        await guard.compensate_instance("i1", TENANT)
