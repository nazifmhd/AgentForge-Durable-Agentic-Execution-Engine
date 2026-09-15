"""Integration fixtures — a real Postgres via the configured DATABASE_URL.

Skips the whole module if the database can't be reached, so ``pytest`` stays
green on a laptop with no infra. CI provides Postgres and runs these.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentforge.config import settings
from agentforge.core.persistence import tables as _tables  # noqa: F401 - registers tables
from agentforge.core.persistence.definition_repo import DefinitionRepository
from agentforge.core.persistence.event_store import EventStore
from agentforge.db import Base

# A session-scoped asyncpg engine needs its connections created and used on one
# and the same event loop for the whole run — pytest-asyncio otherwise hands out
# a *new* loop per test function, and asyncpg then raises "cannot perform
# operation: another operation is in progress" the moment a later test tries to
# use a pooled connection that belongs to an already-closed earlier loop. Pin
# every fixture (and, via the marker below, every test) in this package to the
# same session-scoped loop so the pool stays valid throughout.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _engine() -> AsyncIterator[object]:
    engine = create_async_engine(str(settings.database_url))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - any connection failure -> skip
        await engine.dispose()
        pytest.skip(f"Postgres not available: {exc}")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def sessionmaker(
    _engine: object,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    sm = async_sessionmaker(_engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with sm() as s, s.begin():
        for table in reversed(Base.metadata.sorted_tables):
            await s.execute(table.delete())
    yield sm


@pytest.fixture
def event_store(sessionmaker: async_sessionmaker[AsyncSession]) -> EventStore:
    return EventStore(sessionmaker)


@pytest.fixture
def definitions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> DefinitionRepository:
    return DefinitionRepository(sessionmaker)
