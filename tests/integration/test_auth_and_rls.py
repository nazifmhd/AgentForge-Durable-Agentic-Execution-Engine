from __future__ import annotations

import pytest
from sqlalchemy import text

from agentforge.core.auth import AuthError, AuthService, PgApiKeyStore, mint_api_key
from agentforge.core.persistence.tables import DeadLetterRow

pytestmark = pytest.mark.integration


async def test_pg_api_key_round_trip(sessionmaker) -> None:
    store = PgApiKeyStore(sessionmaker)
    plaintext, record = mint_api_key(tenant_id="t1", name="ci", scopes=["instances:read"])
    await store.create(record)

    principal = await AuthService(store).authenticate(api_key=plaintext, bearer=None)
    assert principal.tenant_id == "t1"
    assert principal.has("instances:read")
    assert not principal.has("admin")

    with pytest.raises(AuthError):
        await AuthService(store).authenticate(api_key=plaintext[:-3] + "xxx", bearer=None)
    with pytest.raises(AuthError):
        await AuthService(store).authenticate(api_key="garbage", bearer=None)


async def _rls_enabled(sessionmaker) -> bool:
    async with sessionmaker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM pg_policies WHERE tablename = 'dead_letters'")
        )
    return bool(n)


async def test_row_level_security_clamps_to_the_guc(sessionmaker) -> None:
    if not await _rls_enabled(sessionmaker):
        pytest.skip("RLS policy not present (fixture uses create_all, not migrations)")

    async with sessionmaker() as s, s.begin():
        for tenant in ("rls-a", "rls-b"):
            s.add(
                DeadLetterRow(
                    instance_id=f"{tenant}-i",
                    tenant_id=tenant,
                    step_id="x",
                    reason="boom",
                    at_version=1,
                )
            )

    async with sessionmaker() as s:
        await s.execute(text("SET LOCAL agentforge.tenant_id = 'rls-a'"))
        rows = (await s.execute(text("SELECT tenant_id FROM dead_letters"))).all()
        assert {r.tenant_id for r in rows} == {"rls-a"}
