from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx
import pytest_asyncio
from tests.doubles import (
    FakeDeadLetters,
    FakeRedis,
    InMemoryApiKeyStore,
    InMemoryDefinitions,
    InMemoryEscalationReadStore,
    InMemoryJournal,
    InMemoryLeaseStore,
)
from tests.factories import T0

from agentforge.api.app import create_app
from agentforge.api.deps import ApiDeps
from agentforge.api.middleware.rate_limit import RateLimiter
from agentforge.core.auth import AuthService, Scope, mint_api_key
from agentforge.core.control import InstanceControl
from agentforge.core.driver import WorkflowDriver
from agentforge.core.escalation import EscalationController
from agentforge.core.executor import StepExecutor
from agentforge.core.instances import InstanceService
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult

TENANT = "acme"


@dataclass(slots=True)
class ApiHarness:
    client: httpx.AsyncClient
    deps: ApiDeps
    journal: InMemoryJournal
    defs: InMemoryDefinitions
    keys: InMemoryApiKeyStore
    redis: FakeRedis
    leases: InMemoryLeaseStore
    driver: WorkflowDriver
    make_key: Callable[..., str]

    async def drive_all(self) -> None:
        while True:
            leases = await self.leases.acquire_runnable("w1", 10, T0)
            if not leases:
                return
            for lease in leases:
                await self.driver.drive(lease, self.leases.make_guard(lease))
                await self.leases.release("w1", lease.instance_id)


@pytest_asyncio.fixture
async def api() -> AsyncIterator[ApiHarness]:
    journal = InMemoryJournal()
    defs = InMemoryDefinitions()
    keys = InMemoryApiKeyStore()
    redis = FakeRedis()
    leases = InMemoryLeaseStore(journal)

    reg = StepRegistry()

    async def echo(ctx: StepContext) -> StepResult:
        return StepResult(output={"did": ctx.step_id})

    reg.register("executor_agent", FunctionRunner(echo))

    driver = WorkflowDriver(
        journal,
        defs,
        StepExecutor(reg),
        FakeDeadLetters(),  # type: ignore[arg-type]
        clock=FixedClock(T0),
        ids=SequentialIdGenerator("ev"),
    )
    dead_letters = FakeDeadLetters()

    deps = ApiDeps(
        auth=AuthService(keys),  # type: ignore[arg-type]
        rate_limiter=RateLimiter(redis),
        definitions=defs,  # type: ignore[arg-type]
        instances=InstanceService(
            journal, defs, clock=FixedClock(T0), ids=SequentialIdGenerator("i")
        ),
        events=journal,  # type: ignore[arg-type]
        escalations=EscalationController(
            InMemoryEscalationReadStore(journal),
            journal,
            clock=FixedClock(T0),
            ids=SequentialIdGenerator("re"),
        ),
        dead_letters=dead_letters,  # type: ignore[arg-type]
        control=InstanceControl(journal, clock=FixedClock(T0), ids=SequentialIdGenerator("c")),
        redis=redis,
    )
    app = create_app(deps)

    def make_key(*scopes: str, tenant: str = TENANT, name: str = "test") -> str:
        plaintext, record = mint_api_key(
            tenant_id=tenant, name=name, scopes=list(scopes) or [Scope.ADMIN]
        )
        keys._by_id[record.key_id] = record
        return plaintext

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield ApiHarness(
            client=client,
            deps=deps,
            journal=journal,
            defs=defs,
            keys=keys,
            redis=redis,
            leases=leases,
            driver=driver,
            make_key=make_key,
        )
