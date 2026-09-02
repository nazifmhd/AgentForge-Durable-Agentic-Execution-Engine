"""Per-tenant fixed-window rate limiting, backed by Redis.

Protective, not correctness-critical: if Redis is unreachable the limiter
**fails open** (logs and allows). Two buckets — a generous default and a tighter
one for workflow starts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agentforge.logging import get_logger

log = get_logger("ratelimit")


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_in: int


class RateLimiter:
    def __init__(self, redis: Any | None) -> None:
        self._redis = redis

    async def check(
        self, tenant_id: str, bucket: str, limit: int, window_seconds: int = 60
    ) -> RateDecision:
        if self._redis is None or limit <= 0:
            return RateDecision(True, limit, limit, window_seconds)
        window = int(time.time()) // window_seconds
        key = f"agentforge:rl:{tenant_id}:{bucket}:{window}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, window_seconds)
        except Exception:  # noqa: BLE001 - fail open
            log.warning("rate_limit_backend_unavailable", tenant_id=tenant_id)
            return RateDecision(True, limit, limit, window_seconds)
        remaining = max(limit - int(count), 0)
        reset_in = window_seconds - (int(time.time()) % window_seconds)
        return RateDecision(int(count) <= limit, limit, remaining, reset_in)
