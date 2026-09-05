"""``fold`` — rebuild a :class:`WorkflowInstance` from its event stream.

Rules:

* The stream (without a base) must open with ``InstanceCreated``.
* ``sequence`` must be contiguous and strictly increasing from the base.
* Status transitions are checked against the tables in ``domain.enums``; an
  illegal transition raises :class:`InvalidStateTransition` rather than being
  applied — a corrupt log should fail loudly.

**Cost accounting:** ``instance.cost_accumulated_usd`` and ``tokens_used`` move
*only* on ``CostCharged`` events (emitted for every billable call, including
failed attempts). ``StepCompleted`` carries an informational per-step figure but
does not itself accrue instance cost.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import (
    StepStatus,
    WorkflowStatus,
    step_transition_allowed,
    workflow_transition_allowed,
)
from agentforge.core.domain.instance import (
    ErrorRecord,
    EscalationRef,
    SideEffectRef,
    StepState,
    WorkflowInstance,
)
from agentforge.core.events import types as E
from agentforge.exceptions import EventStreamError, InvalidStateTransition


def _ensure_step(inst: WorkflowInstance, step_id: str) -> StepState:
    st = inst.step_states.get(step_id)
    if st is None:
        st = StepState(step_id=step_id)
        inst.step_states[step_id] = st
    return st


def _set_step_status(inst: WorkflowInstance, step_id: str, target: StepStatus) -> None:
    st = _ensure_step(inst, step_id)
    if not step_transition_allowed(st.status, target):
        raise InvalidStateTransition(f"step {step_id!r}: {st.status} -> {target} not allowed")
    st.status = target


def _set_workflow_status(
    inst: WorkflowInstance, target: WorkflowStatus, *, declared_from: WorkflowStatus | None
) -> None:
    if declared_from is not None and declared_from != inst.status:
        raise EventStreamError(
            f"stale transition: event says from={declared_from} but instance is {inst.status}"
        )
    if inst.status != target and not workflow_transition_allowed(inst.status, target):
        raise InvalidStateTransition(f"workflow: {inst.status} -> {target} not allowed")
    inst.status = target


# --- per-event handlers ------------------------------------------------
def _on_created(
    inst: WorkflowInstance, e: E.InstanceCreated, defn: WorkflowDefinition | None
) -> None:
    inst.context = dict(e.context)
    inst.budget_limit_usd = e.budget_limit_usd
    inst.created_at = e.occurred_at
    if defn is not None:
        for step in defn.steps:
            inst.step_states.setdefault(step.step_id, StepState(step_id=step.step_id))


def _on_wf_status(inst: WorkflowInstance, e: E.InstanceStatusChanged, _: object) -> None:
    _set_workflow_status(inst, e.to_status, declared_from=e.from_status)
    if inst.is_terminal:
        inst.completed_at = e.occurred_at


def _on_completed(inst: WorkflowInstance, e: E.InstanceCompleted, _: object) -> None:
    _set_workflow_status(inst, WorkflowStatus.COMPLETED, declared_from=None)
    inst.completed_at = e.occurred_at
    if e.outputs:
        inst.context["_outputs"] = e.outputs


def _on_failed(inst: WorkflowInstance, e: E.InstanceFailed, _: object) -> None:
    _set_workflow_status(inst, WorkflowStatus.FAILED, declared_from=None)
    inst.error_history.append(
        ErrorRecord(
            error_type=e.error_type,
            error_message=e.error_message,
            occurred_at=e.occurred_at,
        )
    )


def _on_requeued(inst: WorkflowInstance, e: E.WorkflowRequeued, _: object) -> None:
    _set_workflow_status(inst, WorkflowStatus.RUNNING, declared_from=None)
    if e.step_id is not None:
        st = _ensure_step(inst, e.step_id)
        st.status = StepStatus.READY
        st.attempts = 0
        st.next_retry_at = None
        st.error_type = None
        st.error_message = None


def _on_budget_adjusted(inst: WorkflowInstance, e: E.WorkflowBudgetAdjusted, _: object) -> None:
    inst.budget_limit_usd = e.new_limit_usd


def _on_step_status(inst: WorkflowInstance, e: E.StepStatusChanged, _: object) -> None:
    st = _ensure_step(inst, e.step_id)
    if e.from_status != st.status:
        raise EventStreamError(
            f"step {e.step_id!r}: event from={e.from_status} but state is {st.status}"
        )
    _set_step_status(inst, e.step_id, e.to_status)


def _on_step_started(inst: WorkflowInstance, e: E.StepStarted, _: object) -> None:
    _set_step_status(inst, e.step_id, StepStatus.RUNNING)
    st = inst.step(e.step_id)
    st.attempts = e.attempt
    st.started_at = e.occurred_at
    st.next_retry_at = None
    if e.model_selected:
        st.model_used = e.model_selected


def _on_step_completed(inst: WorkflowInstance, e: E.StepCompleted, _: object) -> None:
    _set_step_status(inst, e.step_id, StepStatus.COMPLETED)
    st = inst.step(e.step_id)
    st.output = dict(e.output)
    st.completed_at = e.occurred_at
    if e.model_used:
        st.model_used = e.model_used


def _on_step_failed(inst: WorkflowInstance, e: E.StepFailed, _: object) -> None:
    _set_step_status(inst, e.step_id, StepStatus.FAILED)
    st = inst.step(e.step_id)
    st.error_type = e.error_type
    st.error_message = e.error_message
    st.recorded_llm_calls.clear()  # a retry re-calls the model fresh
    inst.error_history.append(
        ErrorRecord(
            step_id=e.step_id,
            error_type=e.error_type,
            error_message=e.error_message,
            occurred_at=e.occurred_at,
            attempt=e.attempt,
        )
    )


def _on_retry_scheduled(inst: WorkflowInstance, e: E.StepRetryScheduled, _: object) -> None:
    st = _ensure_step(inst, e.step_id)
    st.next_retry_at = e.run_at


def _on_step_skipped(inst: WorkflowInstance, e: E.StepSkipped, _: object) -> None:
    _set_step_status(inst, e.step_id, StepStatus.SKIPPED)


def _on_step_compensated(inst: WorkflowInstance, e: E.StepCompensated, _: object) -> None:
    _set_step_status(inst, e.step_id, StepStatus.COMPENSATED)


def _on_llm_recorded(inst: WorkflowInstance, e: E.LLMCallRecorded, _: object) -> None:
    st = _ensure_step(inst, e.step_id)
    st.model_used = e.model
    st.recorded_llm_calls.append(
        {
            "request_digest": e.request_digest,
            "model": e.model,
            "response": e.response,
            "tokens_input": e.tokens_input,
            "tokens_output": e.tokens_output,
            "cost_usd": e.cost_usd,
        }
    )


def _on_cost(inst: WorkflowInstance, e: E.CostCharged, _: object) -> None:
    inst.cost_accumulated_usd += e.amount_usd
    inst.tokens_used.input += e.tokens_input
    inst.tokens_used.output += e.tokens_output
    if e.step_id:
        st = _ensure_step(inst, e.step_id)
        st.cost_usd += e.amount_usd
        st.tokens.input += e.tokens_input
        st.tokens.output += e.tokens_output


def _on_budget_exceeded(inst: WorkflowInstance, e: E.BudgetExceeded, _: object) -> None:
    inst.error_history.append(
        ErrorRecord(
            step_id=e.step_id,
            error_type="BudgetExceededError",
            error_message=(
                f"projected {e.projected_cost_usd:.4f} > remaining "
                f"{e.remaining_budget_usd:.4f} ({e.scope})"
            ),
            occurred_at=e.occurred_at,
        )
    )


def _on_escalation_raised(inst: WorkflowInstance, e: E.EscalationRaised, _: object) -> None:
    if e.step_id:  # workflow-level escalations (e.g. compensation failure) carry no step
        _set_step_status(inst, e.step_id, StepStatus.WAITING_APPROVAL)
    inst.escalations.append(
        EscalationRef(
            escalation_id=e.escalation_id,
            step_id=e.step_id,
            reason=e.reason,
            deadline=e.deadline,
        )
    )


def _resolve_escalation(inst: WorkflowInstance, escalation_id: str) -> None:
    for ref in inst.escalations:
        if ref.escalation_id == escalation_id:
            ref.resolved = True


def _on_escalation_resolved(inst: WorkflowInstance, e: E.EscalationResolved, _: object) -> None:
    _resolve_escalation(inst, e.escalation_id)
    if e.modified_context:
        inst.context.update(e.modified_context)


def _on_escalation_timed_out(inst: WorkflowInstance, e: E.EscalationTimedOut, _: object) -> None:
    _resolve_escalation(inst, e.escalation_id)


def _side_effect_ref(inst: WorkflowInstance, key: str) -> SideEffectRef | None:
    for ref in inst.side_effects:
        if ref.idempotency_key == key:
            return ref
    return None


def _on_effect_intent(inst: WorkflowInstance, e: E.SideEffectIntentRecorded, _: object) -> None:
    if _side_effect_ref(inst, e.idempotency_key) is None:
        inst.side_effects.append(
            SideEffectRef(
                idempotency_key=e.idempotency_key,
                step_id=e.step_id,
                effect_name=e.effect_name,
                status="pending",
            )
        )


def _on_effect_executed(inst: WorkflowInstance, e: E.SideEffectExecuted, _: object) -> None:
    ref = _side_effect_ref(inst, e.idempotency_key)
    if ref is None:
        inst.side_effects.append(
            SideEffectRef(
                idempotency_key=e.idempotency_key,
                step_id=e.step_id,
                effect_name="",
                status="executed",
            )
        )
    else:
        ref.status = "executed"


def _on_effect_compensated(inst: WorkflowInstance, e: E.SideEffectCompensated, _: object) -> None:
    ref = _side_effect_ref(inst, e.idempotency_key)
    if ref is not None:
        ref.status = "compensated"


def _noop(inst: WorkflowInstance, e: Any, defn: WorkflowDefinition | None) -> None:
    return None


_Handler = Callable[[WorkflowInstance, Any, "WorkflowDefinition | None"], None]

_HANDLERS: dict[type[E.BaseEvent], _Handler] = {
    E.InstanceCreated: _on_created,
    E.InstanceStatusChanged: _on_wf_status,
    E.InstanceCompleted: _on_completed,
    E.InstanceFailed: _on_failed,
    E.WorkflowRequeued: _on_requeued,
    E.WorkflowBudgetAdjusted: _on_budget_adjusted,
    E.StepStatusChanged: _on_step_status,
    E.StepStarted: _on_step_started,
    E.StepCompleted: _on_step_completed,
    E.StepFailed: _on_step_failed,
    E.StepRetryScheduled: _on_retry_scheduled,
    E.StepSkipped: _on_step_skipped,
    E.StepCompensated: _on_step_compensated,
    E.LLMCallRecorded: _on_llm_recorded,
    E.ToolCallRecorded: _noop,
    E.SideEffectIntentRecorded: _on_effect_intent,
    E.SideEffectExecuted: _on_effect_executed,
    E.SideEffectCompensated: _on_effect_compensated,
    E.EscalationRaised: _on_escalation_raised,
    E.EscalationResolved: _on_escalation_resolved,
    E.EscalationTimedOut: _on_escalation_timed_out,
    E.CostCharged: _on_cost,
    E.BudgetExceeded: _on_budget_exceeded,
}


def fold(
    events: Iterable[E.BaseEvent],
    *,
    definition: WorkflowDefinition | None = None,
    base: WorkflowInstance | None = None,
) -> WorkflowInstance:
    events = list(events)
    inst = base.model_copy(deep=True) if base is not None else None
    expected_seq = (base.version if base is not None else 0) + 1

    for ev in events:
        if ev.sequence != expected_seq:
            raise EventStreamError(
                f"non-contiguous sequence: expected {expected_seq}, got {ev.sequence}"
            )
        expected_seq += 1

        if inst is None:
            if not isinstance(ev, E.InstanceCreated):
                raise EventStreamError(f"first event must be InstanceCreated, got {ev.event_type}")
            inst = WorkflowInstance(
                instance_id=ev.instance_id,
                tenant_id=ev.tenant_id,
                workflow_id=ev.workflow_id,
                workflow_version=ev.workflow_version,
            )

        handler = _HANDLERS.get(type(ev))
        if handler is None:  # pragma: no cover - defensive
            raise EventStreamError(f"no fold handler for {ev.event_type}")
        handler(inst, ev, definition)

        inst.updated_at = ev.occurred_at
        inst.version = ev.sequence

    if inst is None:
        raise EventStreamError("cannot fold an empty stream without a base instance")
    return inst
