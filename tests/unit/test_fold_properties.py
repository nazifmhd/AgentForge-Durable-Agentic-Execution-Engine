"""Property-based invariants for the fold projection.

The strategy simulates a plausible executor: a linear workflow whose steps are
each skipped, or run (with optional failed attempts + retries) to completion.
Whatever sequence it produces, the fold must satisfy the invariants below.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.factories import StreamBuilder, linear_workflow

from agentforge.core.events import fold
from agentforge.core.events import types as E
from agentforge.core.events.snapshot import Snapshot


@st.composite
def event_streams(draw: st.DrawFn) -> tuple[list[E.BaseEvent], int]:
    n_steps = draw(st.integers(min_value=1, max_value=5))
    b = StreamBuilder()
    b.created(workflow_id="wf-linear", workflow_version="1.0.0", budget_limit_usd=100.0)
    b.wf_status("pending", "running")

    for i in range(1, n_steps + 1):
        step = f"step_{i}"
        if draw(st.booleans()):
            b.step_status(step, "pending", "skipped")
            continue
        b.step_status(step, "pending", "ready")
        attempt = 1
        for _ in range(draw(st.integers(min_value=0, max_value=3))):  # failed attempts
            b.step_started(step, attempt=attempt)
            if draw(st.booleans()):
                b.cost(
                    draw(st.floats(min_value=0, max_value=0.5)),
                    step_id=step,
                    tokens_input=draw(st.integers(0, 100)),
                    tokens_output=draw(st.integers(0, 100)),
                )
            b.step_failed(step, attempt=attempt)
            b.step_status(step, "failed", "ready")
            attempt += 1
        b.step_started(step, attempt=attempt)
        b.cost(
            draw(st.floats(min_value=0, max_value=0.5)),
            step_id=step,
            tokens_input=draw(st.integers(0, 100)),
            tokens_output=draw(st.integers(0, 100)),
        )
        b.step_completed(step, attempt=attempt)

    b.raw(E.InstanceCompleted, outputs={"n": n_steps})
    return b.events, n_steps


@settings(max_examples=200, deadline=None)
@given(event_streams())
def test_fold_is_deterministic(data: tuple[list[E.BaseEvent], int]) -> None:
    events, n = data
    defn = linear_workflow(n)
    assert fold(events, definition=defn).model_dump() == fold(events, definition=defn).model_dump()


@settings(max_examples=200, deadline=None)
@given(event_streams())
def test_version_tracks_last_sequence(data: tuple[list[E.BaseEvent], int]) -> None:
    events, _ = data
    for i in range(1, len(events) + 1):
        assert fold(events[:i]).version == events[i - 1].sequence


@settings(max_examples=200, deadline=None)
@given(event_streams())
def test_cost_is_monotonic_and_equals_sum_of_charges(
    data: tuple[list[E.BaseEvent], int],
) -> None:
    events, _ = data
    prev = -1.0
    for i in range(1, len(events) + 1):
        cost = fold(events[:i]).cost_accumulated_usd
        assert cost >= prev - 1e-9
        prev = cost
    charged = sum(e.amount_usd for e in events if isinstance(e, E.CostCharged))
    assert abs(fold(events).cost_accumulated_usd - charged) < 1e-6


@settings(max_examples=200, deadline=None)
@given(event_streams(), st.integers(min_value=1, max_value=40))
def test_incremental_fold_matches_full_fold(
    data: tuple[list[E.BaseEvent], int], split: int
) -> None:
    events, n = data
    defn = linear_workflow(n)
    k = min(split, len(events))
    base = fold(events[:k], definition=defn)
    snap = Snapshot.of(base, created_at=base.updated_at or base.created_at)
    resumed = fold(events[k:], base=snap.state, definition=defn)
    assert resumed.model_dump() == fold(events, definition=defn).model_dump()
