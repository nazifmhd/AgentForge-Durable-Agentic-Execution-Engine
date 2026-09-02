"""Composition root — wire the concrete engine from settings.

Kept separate from ``config`` (values) and the components themselves (logic) so
tests can assemble their own graphs with doubles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentforge.config import settings
from agentforge.core.cost.budget import BudgetService, PgBudgetLedger
from agentforge.core.cost.registry import ModelRegistry
from agentforge.core.cost.router import CostAwareRouter
from agentforge.core.dead_letter import DeadLetterService
from agentforge.core.driver import WorkflowDriver
from agentforge.core.escalation import EscalationController, PgEscalationReadStore
from agentforge.core.executor import StepExecutor
from agentforge.core.instances import InstanceService
from agentforge.core.leasing import PgLeaseStore
from agentforge.core.llm_client import LLMClient
from agentforge.core.outbox import PgOutboxStore
from agentforge.core.persistence.definition_repo import DefinitionRepository
from agentforge.core.persistence.event_store import EventStore
from agentforge.core.pubsub import NoopPublisher, RedisEventPublisher
from agentforge.core.recovery import RecoveryService
from agentforge.core.runners import StepRegistry, default_registry
from agentforge.core.side_effects import SideEffectGuard
from agentforge.db import get_sessionmaker
from agentforge.integrations.actions import NoopActionProvider, ProviderRegistry
from agentforge.integrations.llm import LLMProviderRegistry, build_provider
from agentforge.integrations.notifications import (
    LogNotifier,
    MultiNotifier,
    Notifier,
    WebhookNotifier,
)
from agentforge.logging import get_logger
from agentforge.redis_client import get_redis
from agentforge.worker import Worker

log = get_logger("bootstrap")


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
    models: ModelRegistry
    router: CostAwareRouter
    llm: LLMClient
    budget: BudgetService
    escalations: EscalationController
    notifier: Notifier


def _build_llm_providers() -> LLMProviderRegistry:
    reg = LLMProviderRegistry()
    if settings.anthropic_api_key:
        reg.register(build_provider("anthropic", api_key=settings.anthropic_api_key))
    if settings.openai_api_key:
        reg.register(build_provider("openai", api_key=settings.openai_api_key))
    if "anthropic" not in reg and "openai" not in reg:
        log.warning("no_llm_provider_keys_configured")
    return reg


def _build_notifier() -> Notifier:
    channels: dict[str, str] = {}
    if settings.n8n_base_url:
        channels["escalations"] = f"{settings.n8n_base_url}/webhook/notify-escalations"
    if channels:
        return MultiNotifier(LogNotifier(), WebhookNotifier(channels))
    return LogNotifier()


def build_engine(
    registry: StepRegistry | None = None,
    providers: ProviderRegistry | None = None,
) -> Engine:
    sm = get_sessionmaker()
    if registry is None:
        registry = default_registry()
        from agentforge.agents import register_base_agents

        register_base_agents(registry)
    if providers is None:
        providers = ProviderRegistry()
        providers.register(NoopActionProvider())

    models = ModelRegistry.from_path(settings.model_registry_path)
    router = CostAwareRouter(models)
    llm_providers = _build_llm_providers()
    llm = LLMClient(router, models, llm_providers)
    budget = BudgetService(PgBudgetLedger(sm), tenant_daily_limit_usd=settings.org_daily_budget_usd)
    notifier = _build_notifier()

    publisher: RedisEventPublisher | NoopPublisher
    try:
        publisher = RedisEventPublisher(get_redis())
    except Exception:  # noqa: BLE001 - degrade to no live updates
        log.warning("redis_publisher_unavailable")
        publisher = NoopPublisher()

    events = EventStore(sm, publisher=publisher)
    definitions = DefinitionRepository(sm)
    dead_letters = DeadLetterService(sm)
    leases = PgLeaseStore(sm, lease_seconds=settings.lease_seconds)
    recovery = RecoveryService(sm, stale_after_seconds=settings.recovery_scan_interval_seconds * 4)
    side_effects = SideEffectGuard(PgOutboxStore(sm), providers)
    escalations = EscalationController(PgEscalationReadStore(sm), events, notifier=notifier)
    driver = WorkflowDriver(
        events,
        definitions,
        StepExecutor(registry),
        dead_letters,
        side_effects=side_effects,
        llm=llm,
        budget=budget,
        notifier=notifier,
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
        models=models,
        router=router,
        llm=llm,
        budget=budget,
        escalations=escalations,
        notifier=notifier,
    )


def build_api_deps(engine: Engine | None = None) -> Any:
    """Assemble the ``ApiDeps`` bundle the FastAPI app reads from ``app.state``."""
    from agentforge.api.deps import ApiDeps
    from agentforge.api.middleware.rate_limit import RateLimiter
    from agentforge.core.auth import AuthService, PgApiKeyStore
    from agentforge.core.control import InstanceControl

    engine = engine or build_engine()
    sm = get_sessionmaker()
    try:
        redis: Any | None = get_redis()
    except Exception:  # noqa: BLE001
        log.warning("redis_unavailable_for_api")
        redis = None

    return ApiDeps(
        auth=AuthService(PgApiKeyStore(sm)),
        rate_limiter=RateLimiter(redis),
        definitions=engine.definitions,
        instances=engine.instances,
        events=engine.events,
        escalations=engine.escalations,
        dead_letters=engine.dead_letters,
        control=InstanceControl(engine.events),
        redis=redis,
    )


def build_worker(engine: Engine | None = None) -> Worker:
    engine = engine or build_engine()
    return Worker(
        engine.leases,
        engine.driver,
        concurrency=settings.max_concurrent_steps_per_worker,
        heartbeat_seconds=settings.lease_heartbeat_seconds,
        recovery_interval_seconds=settings.recovery_scan_interval_seconds,
        escalations=engine.escalations,
    )
