"""Shared async Redis client (pub/sub for live views, rate-limit counters)."""

from __future__ import annotations

from redis.asyncio import Redis

from agentforge.config import settings

_redis: Redis | None = None  # type: ignore[type-arg]


def get_redis() -> Redis:  # type: ignore[type-arg]
    global _redis
    if _redis is None:
        _redis = Redis.from_url(str(settings.redis_url), decode_responses=False)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()  # type: ignore[attr-defined]
    _redis = None
