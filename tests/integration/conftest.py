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

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(scope="session")
async def _engine() -> AsyncIterator[object]:
    engine = create_async_engine(str(settings.database_url))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"Postgres not available: {exc}")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
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
