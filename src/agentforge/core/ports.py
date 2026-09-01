"""Injectable ports for non-deterministic inputs (ADR-0005).

``core`` code and agents must take a ``Clock`` / ``IdGenerator`` rather than
calling ``datetime.now()`` / ``uuid4()`` directly, so that replay can substitute
recording/replaying implementations. A ruff rule (Phase 2) bans the direct calls
in these packages.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class IdGenerator(Protocol):
    def new_id(self) -> str: ...


class SystemClock:
    """Wall-clock time, always timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Deterministic clock for tests / replay. ``tick`` advances it."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def tick(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, value: datetime) -> None:
        self._now = value if value.tzinfo else value.replace(tzinfo=UTC)


class UuidGenerator:
    def new_id(self) -> str:
        return str(uuid.uuid4())


class SequentialIdGenerator:
    """Predictable ids for tests: ``prefix-1``, ``prefix-2``, …"""

    def __init__(self, prefix: str = "id") -> None:
        self._prefix = prefix
        self._counter = itertools.count(1)

    def new_id(self) -> str:
        return f"{self._prefix}-{next(self._counter)}"


SYSTEM_CLOCK = SystemClock()
UUID_GENERATOR = UuidGenerator()
