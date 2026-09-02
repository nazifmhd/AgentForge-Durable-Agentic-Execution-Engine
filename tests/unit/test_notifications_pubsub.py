from __future__ import annotations

import orjson
import pytest
from tests.factories import T0

from agentforge.core.events.types import StepCompleted
from agentforge.core.pubsub import InstanceUpdate, NoopPublisher, RedisEventPublisher
from agentforge.integrations.notifications import (
    LogNotifier,
    MultiNotifier,
    Notification,
    WebhookNotifier,
)


class _FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, bytes]] = []
        self._fail = fail

    async def publish(self, channel: str, data: bytes) -> None:
        if self._fail:
            raise RuntimeError("redis down")
        self.published.append((channel, data))


def _event(seq: int) -> StepCompleted:
    return StepCompleted(
        event_id=f"e{seq}",
        instance_id="i1",
        tenant_id="t1",
        sequence=seq,
        occurred_at=T0,
        step_id="s",
        attempt=1,
    )


async def test_redis_publisher_emits_compact_update() -> None:
    redis = _FakeRedis()
    pub = RedisEventPublisher(redis)
    await pub.publish("i1", "t1", [_event(4), _event(5)])

    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    assert channel == "agentforge:instance:i1"
    body = orjson.loads(payload)
    assert body["version"] == 5
    assert body["events"] == ["StepCompleted", "StepCompleted"]


async def test_redis_publisher_swallows_bus_errors() -> None:
    pub = RedisEventPublisher(_FakeRedis(fail=True))
    await pub.publish("i1", "t1", [_event(1)])  # must not raise


async def test_noop_publisher_is_inert() -> None:
    await NoopPublisher().publish("i1", "t1", [_event(1)])


def test_instance_update_json_shape() -> None:
    body = orjson.loads(
        InstanceUpdate(instance_id="i", tenant_id="t", version=3, event_types=["A"]).to_json()
    )
    assert body == {"instance_id": "i", "tenant_id": "t", "version": 3, "events": ["A"]}


# --- notifications ---------------------------------------------------
async def test_log_notifier_does_not_raise() -> None:
    await LogNotifier().notify(Notification(channel="x", subject="s", body="b"))


async def test_multi_notifier_continues_after_one_fails() -> None:
    seen: list[str] = []

    class Bad:
        async def notify(self, note: Notification) -> None:
            raise RuntimeError("boom")

    class Good:
        async def notify(self, note: Notification) -> None:
            seen.append(note.subject)

    await MultiNotifier(Bad(), Good()).notify(Notification(channel="x", subject="hi", body="b"))
    assert seen == ["hi"]


async def test_webhook_notifier_posts_json(respx_mock) -> None:  # respx fixture
    import httpx

    route = respx_mock.post("https://hook.example/esc").mock(return_value=httpx.Response(200))
    n = WebhookNotifier({"escalations": "https://hook.example/esc"})
    await n.notify(Notification(channel="escalations", subject="s", body="b", metadata={"k": 1}))
    assert route.called
    sent = orjson.loads(route.calls[0].request.content)
    assert sent["subject"] == "s"
    assert sent["metadata"] == {"k": 1}


async def test_webhook_notifier_unknown_channel_is_noop() -> None:
    await WebhookNotifier({}).notify(Notification(channel="ghost", subject="s", body="b"))


@pytest.fixture
def respx_mock():
    import respx

    with respx.mock(assert_all_called=False) as mock:
        yield mock
