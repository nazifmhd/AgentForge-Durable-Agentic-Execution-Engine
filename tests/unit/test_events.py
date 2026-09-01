from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentforge.core.events import EVENT_TYPES, dump_event, parse_event
from agentforge.core.events import types as E


def _kw(**extra: object) -> dict[str, object]:
    base = {
        "event_id": "e1",
        "instance_id": "i1",
        "tenant_id": "t1",
        "sequence": 1,
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(extra)
    return base


def test_every_event_type_is_registered() -> None:
    # 21 concrete events in the union
    assert len(EVENT_TYPES) == 21
    assert EVENT_TYPES["StepCompleted"] is E.StepCompleted


def test_discriminated_roundtrip() -> None:
    ev = E.StepCompleted(**_kw(step_id="s1", attempt=1, output={"k": "v"}, cost_usd=0.01))
    restored = parse_event(dump_event(ev))
    assert isinstance(restored, E.StepCompleted)
    assert restored == ev


def test_parse_dispatches_on_event_type() -> None:
    payload = dump_event(E.InstanceCreated(**_kw(workflow_id="w", workflow_version="1.0.0")))
    assert isinstance(parse_event(payload), E.InstanceCreated)


def test_events_are_frozen() -> None:
    ev = E.StepSkipped(**_kw(step_id="s", reason="dep failed"))
    with pytest.raises(Exception):  # noqa: B017 - pydantic frozen error
        ev.reason = "x"  # type: ignore[misc]


def test_sequence_must_be_positive() -> None:
    with pytest.raises(Exception):  # noqa: B017
        E.StepSkipped(**_kw(sequence=0, step_id="s", reason="r"))


def test_unknown_event_type_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017
        parse_event({**_kw(), "event_type": "NotAThing"})
