"""Composition root — wire the concrete engine from settings.

Kept separate from ``config`` (values) and the components themselves (logic) so
tests can assemble their own graphs with doubles.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentforge.config import settings
from agentforge.core.dead_letter import DeadLetterService
from agentforge.core.driver import WorkflowDriver
from agentforge.core.executor import StepExecutor
from agentforge.core.instances import InstanceService
from agentforge.core.leasing import PgLeaseStore
from agentforge.core.outbox import PgOutboxStore
from agentforge.core.persistence.definition_repo import DefinitionRepository
from agentforge.core.persistence.event_store import EventStore
from agentforge.core.recovery import RecoveryService
from agentforge.core.runners import StepRegistry, default_registry
from agentforge.core.side_effects import SideEffectGuard
from agentforge.db import get_sessionmaker
from agentforge.integrations.actions import NoopActionProvider, ProviderRegistry
from agentforge.worker import Worker


@dataclass(slots=True)
class Engine:
    events: EventStore
    definitions: DefinitionRepository
    instances: InstanceService
    dead_letters: DeadLetterService
    leases: PgLeaseStore
    recovery: RecoveryService
    driver: WorkflowDriver
    registry: StepRegistry
    providers: ProviderRegistry
    side_effects: SideEffectGuard


def build_engine(
    registry: StepRegistry | None = None,
    providers: ProviderRegistry | None = None,
) -> Engine:
    sm = get_sessionmaker()
    registry = registry or default_registry()
    if providers is None:
        providers = ProviderRegistry()
        providers.register(NoopActionProvider())

    events = EventStore(sm)
    definitions = DefinitionRepository(sm)
    dead_letters = DeadLetterService(sm)
    leases = PgLeaseStore(sm, lease_seconds=settings.lease_seconds)
    recovery = RecoveryService(sm, stale_after_seconds=settings.recovery_scan_interval_seconds * 4)
    side_effects = SideEffectGuard(PgOutboxStore(sm), providers)
    driver = WorkflowDriver(
        events,
        definitions,
        StepExecutor(registry),
        dead_letters,
        side_effects=side_effects,
    )
    return Engine(
        events=events,
        definitions=definitions,
        instances=InstanceService(events, definitions),
        dead_letters=dead_letters,
        leases=leases,
        recovery=recovery,
        driver=driver,
        registry=registry,
        providers=providers,
        side_effects=side_effects,
    )


def build_worker(engine: Engine | None = None) -> Worker:
    engine = engine or build_engine()
    return Worker(
        engine.leases,
        engine.driver,
        concurrency=settings.max_concurrent_steps_per_worker,
        heartbeat_seconds=settings.lease_heartbeat_seconds,
        recovery_interval_seconds=settings.recovery_scan_interval_seconds,
    )
