"""Event fan-out for live views.

After an append commits, the event store best-effort-publishes a compact update
to a pub/sub bus. The FastAPI WebSocket endpoint (Phase 6) subscribes an
:class:`InstanceStream` to relay updates to a browser. Publishing is never on the
durability path — a bus outage degrades to "no live updates", never to lost state.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import orjson

from agentforge.core.events import BaseEvent
from agentforge.logging import get_logger

log = get_logger("pubsub")


@dataclass(frozen=True, slots=True)
class InstanceUpdate:
    instance_id: str
    tenant_id: str
    version: int
    event_types: list[str]

    def to_json(self) -> bytes:
        return orjson.dumps(
            {
                "instance_id": self.instance_id,
                "tenant_id": self.tenant_id,
                "version": self.version,
                "events": self.event_types,
            }
        )


class EventPublisher(Protocol):
    async def publish(
        self, instance_id: str, tenant_id: str, events: Sequence[BaseEvent]
    ) -> None: ...


class NoopPublisher:
    async def publish(self, instance_id: str, tenant_id: str, events: Sequence[BaseEvent]) -> None:
        return None


def _channel(instance_id: str) -> str:
    return f"agentforge:instance:{instance_id}"


class RedisEventPublisher:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def publish(self, instance_id: str, tenant_id: str, events: Sequence[BaseEvent]) -> None:
        if not events:
            return
        update = InstanceUpdate(
            instance_id=instance_id,
            tenant_id=tenant_id,
            version=events[-1].sequence,
            event_types=[e.event_type for e in events],
        )
        try:
            await self._redis.publish(_channel(instance_id), update.to_json())
        except Exception:  # noqa: BLE001 - live updates are best effort
            log.warning("publish_failed", instance_id=instance_id)


class InstanceStream:
    """Async iterator of updates for one instance, backed by a Redis subscription."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def subscribe(self, instance_id: str) -> AsyncIterator[dict[str, Any]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_channel(instance_id))
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message["data"]
                if isinstance(data, bytes):
                    yield orjson.loads(data)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(_channel(instance_id))
                await pubsub.aclose()
