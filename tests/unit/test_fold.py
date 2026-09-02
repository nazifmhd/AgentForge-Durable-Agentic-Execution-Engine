from __future__ import annotations

import pytest
from tests.factories import T0, StreamBuilder, linear_workflow

from agentforge.core.domain.enums import StepStatus, WorkflowStatus
from agentforge.core.events import fold
from agentforge.core.events import types as E
from agentforge.core.events.snapshot import Snapshot
from agentforge.exceptions import EventStreamError, InvalidStateTransition


def _run_step(b: StreamBuilder, step_id: str, *, cost: float = 0.0) -> None:
    b.step_status(step_id, "pending", "ready")
    b.step_started(step_id)
    if cost:
        b.cost(cost, step_id=step_id, tokens_input=100, tokens_output=50)
    b.step_completed(step_id, output={"ok": True})


def happy_linear() -> StreamBuilder:
    b = StreamBuilder()
    b.created(workflow_id="wf-linear", workflow_version="1.0.0", budget_limit_usd=1.0)
    b.wf_status("pending", "running")
    for i in (1, 2, 3):
        _run_step(b, f"step_{i}", cost=0.10)
    b.raw(E.InstanceCompleted, outputs={"result": "done"})
    return b


def test_genesis_seeds_step_states() -> None:
    b = StreamBuilder().created(workflow_id="wf-linear", workflow_version="1.0.0")
    inst = fold(b.events, definition=linear_workflow(3))
    assert inst.status is WorkflowStatus.PENDING
    assert set(inst.step_states) == {"step_1", "step_2", "step_3"}
    assert all(s.status is StepStatus.PENDING for s in inst.step_states.values())
    assert inst.version == 1
    assert inst.created_at == T0


def test_happy_path_completes() -> None:
    inst = fold(happy_linear().events, definition=linear_workflow(3))
    assert inst.status is WorkflowStatus.COMPLETED
    assert inst.completed_at is not None
    assert [s.status for s in inst.step_states.values()] == [StepStatus.COMPLETED] * 3
    assert inst.context["_outputs"] == {"result": "done"}


def test_cost_accrues_only_via_cost_charged() -> None:
    inst = fold(happy_linear().events, definition=linear_workflow(3))
    assert inst.cost_accumulated_usd == pytest.approx(0.30)
    assert inst.tokens_used.input == 300
    assert inst.tokens_used.output == 150
    assert inst.step("step_1").cost_usd == pytest.approx(0.10)


def test_step_failure_is_recorded() -> None:
    b = StreamBuilder().created().wf_status("pending", "running")
    b.step_status("step_1", "pending", "ready").step_started("step_1")
    b.step_failed("step_1", error_type="RateLimitError", error_message="429")
    inst = fold(b.events, definition=linear_workflow(2))
    assert inst.step("step_1").status is StepStatus.FAILED
    assert inst.error_history[0].error_type == "RateLimitError"
    assert inst.error_history[0].step_id == "step_1"


def test_retry_then_success_across_attempts() -> None:
    b = StreamBuilder().created().wf_status("pending", "running")
    b.step_status("step_1", "pending", "ready").step_started("step_1", attempt=1)
    b.cost(0.05, step_id="step_1", tokens_input=10, tokens_output=5)
    b.step_failed("step_1", attempt=1)
    b.raw(E.StepRetryScheduled, step_id="step_1", next_attempt=2, run_at=T0)
    b.step_status("step_1", "failed", "ready").step_started("step_1", attempt=2)
    b.cost(0.05, step_id="step_1", tokens_input=10, tokens_output=5)
    b.step_completed("step_1", attempt=2)
    inst = fold(b.events, definition=linear_workflow(1))
    assert inst.step("step_1").status is StepStatus.COMPLETED
    assert inst.step("step_1").attempts == 2
    assert inst.cost_accumulated_usd == pytest.approx(0.10)  # both attempts billed
    assert inst.step("step_1").cost_usd == pytest.approx(0.10)


def test_illegal_workflow_transition_raises() -> None:
    b = StreamBuilder().created().wf_status("pending", "completed")
    with pytest.raises(InvalidStateTransition):
        fold(b.events)


def test_stale_from_status_raises() -> None:
    b = StreamBuilder().created().wf_status("running", "completed")
    with pytest.raises(EventStreamError, match="stale"):
        fold(b.events)


def test_non_contiguous_sequence_raises() -> None:
    b = StreamBuilder().created()
    gap = E.InstanceStatusChanged(
        event_id="x",
        instance_id="inst-1",
        tenant_id="tenant-1",
        sequence=5,
        occurred_at=T0,
        from_status="pending",
        to_status="running",
    )
    with pytest.raises(EventStreamError, match="contiguous"):
        fold([*b.events, gap])


def test_first_event_must_be_genesis() -> None:
    ev = E.InstanceStatusChanged(
        event_id="x",
        instance_id="i",
        tenant_id="t",
        sequence=1,
        occurred_at=T0,
        from_status="pending",
        to_status="running",
    )
    with pytest.raises(EventStreamError, match="InstanceCreated"):
        fold([ev])


def test_fold_from_snapshot_base_continues() -> None:
    full = happy_linear().events
    head = fold(full[:4], definition=linear_workflow(3))  # through step_1 running
    snap = Snapshot.of(head, created_at=T0)
    tail = full[4:]
    resumed = fold(tail, base=snap.state, definition=linear_workflow(3))
    from_scratch = fold(full, definition=linear_workflow(3))
    assert resumed.model_dump() == from_scratch.model_dump()


def test_escalation_lifecycle() -> None:
    b = StreamBuilder().created().wf_status("pending", "running")
    b.step_status("step_1", "pending", "ready").step_started("step_1")
    b.raw(
        E.EscalationRaised,
        escalation_id="esc-1",
        step_id="step_1",
        reason="low_confidence",
    )
    inst = fold(b.events, definition=linear_workflow(2))
    assert inst.step("step_1").status is StepStatus.WAITING_APPROVAL
    assert inst.escalations[0].resolved is False

    b.raw(
        E.EscalationResolved,
        escalation_id="esc-1",
        step_id="step_1",
        resolution="approve",
        resolved_by="alice",
        modified_context={"note": "ok"},
    )
    inst2 = fold(b.events, definition=linear_workflow(2))
    assert inst2.escalations[0].resolved is True
    assert inst2.context["note"] == "ok"


def test_budget_adjusted_event_updates_limit() -> None:
    b = StreamBuilder().created(
        workflow_id="wf-linear", workflow_version="1.0.0", budget_limit_usd=0.5
    )
    b.raw(E.WorkflowBudgetAdjusted, new_limit_usd=9.0, adjusted_by="ops")
    inst = fold(b.events, definition=linear_workflow(1))
    assert inst.budget_limit_usd == 9.0


def test_ready_steps_and_settled_helpers() -> None:
    defn = linear_workflow(3)
    b = StreamBuilder().created().wf_status("pending", "running")
    _run_step(b, "step_1")
    inst = fold(b.events, definition=defn)
    assert inst.ready_steps(defn) == ["step_2"]
    assert inst.all_steps_settled(defn) is False
