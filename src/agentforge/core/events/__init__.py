"""Event sourcing: the event vocabulary, the fold projection, and snapshots."""

from agentforge.core.events.fold import fold
from agentforge.core.events.snapshot import SNAPSHOT_EVERY, Snapshot, should_snapshot
from agentforge.core.events.types import (
    EVENT_TYPES,
    AnyEvent,
    BaseEvent,
    EventAdapter,
    dump_event,
    parse_event,
)

__all__ = [
    "EVENT_TYPES",
    "SNAPSHOT_EVERY",
    "AnyEvent",
    "BaseEvent",
    "EventAdapter",
    "Snapshot",
    "dump_event",
    "fold",
    "parse_event",
    "should_snapshot",
]
