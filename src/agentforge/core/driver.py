"""``WorkflowDriver`` — advance one leased instance as far as it will go.

One ``drive(lease)`` call:

1. resets any step left ``RUNNING`` by a dead worker back to ``READY`` (recovery);
2. starts a ``PENDING`` / wakes a ``RETRYING`` instance;
3. dispatches every runnable step (respecting ``max_concurrent_steps``), running
   the attempts concurrently and then appending their events;
4. schedules retries with backoff, parks on retry timers, escalates steps that
   need approval, applies the ``on_failure`` policy, or completes the workflow.

Every append carries the lease guard, so a worker that lost its lease cannot
write (ADR-0004). The driver keeps the instance projection in memory and folds
appended events onto it locally — it is the sole writer for the duration of the
drive (the lease guarantees that), so no re-read or per-instance lock is needed.
The worker must not call ``drive`` concurrently for the same instance.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from agentforge.core.cost.budget import BudgetService
from agentforge.core.dead_letter import DeadLetterService
from agentforge.core.domain.definition import WorkflowDefinition, WorkflowStep
from agentforge.core.domain.enums import OnFailure, StepStatus, WorkflowStatus
from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.events import BaseEvent, fold
from agentforge.core.events import types as E
from agentforge.core.executor import StepExecutor, StepOutcome, StepSuccess
from agentforge.core.leasing import Guard, Lease
from agentforge.core.llm_client import LLMClient
from agentforge.core.persistence.protocols import DefinitionSource, EventJournal
from agentforge.core.ports import SYSTEM_CLOCK, UUID_GENERATOR, Clock, IdGenerator
from agentforge.core.runners import ReviewRequest, StepContext
from agentforge.core.side_effects import SideEffectGuard
from agentforge.exceptions import (
    CompensationError,
    ConfigurationError,
    ConflictError,
    LeaseLostError,
)
from agentforge.integrations.notifications import Notification, Notifier
from agentforge.logging import get_logger
from agentforge.observability import metrics
from agentforge.observability.tracing import span

log = get_logger("driver")

_DONE = (StepStatus.COMPLETED, StepStatus.SKIPPED)
_SETTLED = (StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.COMPENSATED)
_MAX_DRIVE_ITERATIONS = 1000


class DriveResult(StrEnum):
    COMPLETED = "completed"
    PAUSED = "paused"
    DEAD_LETTERED = "dead_lettered"
    ROLLED_BACK = "rolled_back"
    PARKED = "parked"
    WAITING_APPROVAL = "waiting_approval"
    LEASE_LOST = "lease_lost"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class DriveReport:
    instance_id: str
    result: DriveResult
    next_wakeup_at: datetime | None = None


@dataclass(slots=True)
class _Plan:
    dispatch: list[tuple[str, int]] = field(default_factory=list)  # (step_id, attempt)
    approval_needed: list[str] = field(default_factory=list)
    terminal_failure: tuple[str, str] | None = None  # (step_id, reason)
    retry_wakeup: datetime | None = None
    all_settled: bool = False
    budget_blocked_step: str | None = None


class WorkflowDriver:
    def __init__(
        self,
        journal: EventJournal,
        definitions: DefinitionSource,
        executor: StepExecutor,
        dead_letters: DeadLetterService,
        *,
        side_effects: SideEffectGuard | None = None,
        llm: LLMClient | None = None,
        budget: BudgetService | None = None,
        notifier: Notifier | None = None,
        clock: Clock = SYSTEM_CLOCK,
        ids: IdGenerator = UUID_GENERATOR,
    ) -> None:
        self._journal = journal
        self._definitions = definitions
        self._executor = executor
        self._dead_letters = dead_letters
        self._side_effects = side_effects
        self._llm = llm
        self._budget = budget
        self._notifier = notifier
        self._clock = clock
        self._ids = ids

    async def _notify(self, instance: WorkflowInstance, reason: str, detail: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.notify(
                Notification(
                    channel="escalations",
                    subject=f"Approval needed: {reason} on {instance.instance_id}",
                    body=detail,
                    metadata={
                        "instance_id": instance.instance_id,
                        "tenant_id": instance.tenant_id,
                        "reason": reason,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - notification is best effort
            log.warning("escalation_notify_failed", instance_id=instance.instance_id)

    async def drive(self, lease: Lease, guard: Guard) -> DriveReport:
        with span(
            "workflow.drive",
            instance_id=lease.instance_id,
            workflow_id=lease.workflow_id,
            worker_id=lease.worker_id,
        ):
            report = await self._drive(lease, guard)
        metrics.record_drive(report.result.value)
        return report

    async def _drive(self, lease: Lease, guard: Guard) -> DriveReport:
        definition = await self._definitions.get(
            lease.workflow_id, lease.workflow_version, tenant_id=lease.tenant_id
        )
        if definition is None:
            raise ConfigurationError(
                f"definition {lease.workflow_id} v{lease.workflow_version} missing"
            )
        instance = await self._journal.get_instance(
            lease.instance_id, lease.tenant_id, definition=definition
        )
        if instance is None:
            return DriveReport(lease.instance_id, DriveResult.IDLE)

        try:
            instance = await self._recover_in_flight(instance, definition, guard)
            return await self._run(instance, definition, lease, guard)
        except (LeaseLostError, ConflictError):
            log.warning("lease_lost", instance_id=lease.instance_id)
            return DriveReport(lease.instance_id, DriveResult.LEASE_LOST)

    # --- main loop ----------------------------------------------------
    async def _run(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        lease: Lease,
        guard: Guard,
    ) -> DriveReport:
        for _ in range(_MAX_DRIVE_ITERATIONS):
            status = instance.status
            if status == WorkflowStatus.COMPLETED:
                return DriveReport(instance.instance_id, DriveResult.COMPLETED)
            if status == WorkflowStatus.ROLLED_BACK:
                return DriveReport(instance.instance_id, DriveResult.ROLLED_BACK)
            if status == WorkflowStatus.PAUSED:
                return DriveReport(instance.instance_id, DriveResult.PAUSED)
            if status == WorkflowStatus.DEAD_LETTERED:
                return DriveReport(instance.instance_id, DriveResult.DEAD_LETTERED)
            if status == WorkflowStatus.WAITING_APPROVAL:
                return DriveReport(instance.instance_id, DriveResult.WAITING_APPROVAL)
            if status == WorkflowStatus.FAILED:
                return DriveReport(instance.instance_id, DriveResult.PAUSED)

            if status == WorkflowStatus.PENDING:
                instance = await self._transition(
                    instance, definition, WorkflowStatus.PENDING, WorkflowStatus.RUNNING, guard
                )
                continue
            if status == WorkflowStatus.RETRYING:
                instance = await self._transition(
                    instance, definition, WorkflowStatus.RETRYING, WorkflowStatus.RUNNING, guard
                )
                continue

            plan = self._plan(instance, definition, self._clock.now())

            if plan.budget_blocked_step is not None:
                return await self._block_on_budget(
                    instance, definition, plan.budget_blocked_step, guard
                )
            if plan.approval_needed:
                return await self._escalate_for_approval(
                    instance, definition, plan.approval_needed, guard
                )
            if plan.terminal_failure is not None:
                return await self._apply_failure_policy(
                    instance, definition, plan.terminal_failure, guard
                )
            if plan.dispatch:
                instance = await self._dispatch_wave(
                    instance, definition, plan.dispatch, lease, guard
                )
                continue
            if plan.all_settled:
                return await self._complete(instance, definition, guard)
            if plan.retry_wakeup is not None:
                instance = await self._transition(
                    instance,
                    definition,
                    WorkflowStatus.RUNNING,
                    WorkflowStatus.RETRYING,
                    guard,
                    next_wakeup_at=plan.retry_wakeup,
                )
                return DriveReport(
                    instance.instance_id,
                    DriveResult.PARKED,
                    next_wakeup_at=plan.retry_wakeup,
                )
            return DriveReport(instance.instance_id, DriveResult.IDLE)

        raise RuntimeError(f"drive did not converge for {instance.instance_id}")

    # --- planning ----------------------------------------------------
    def _plan(
        self, instance: WorkflowInstance, definition: WorkflowDefinition, now: datetime
    ) -> _Plan:
        plan = _Plan()
        running = sum(1 for s in instance.step_states.values() if s.status == StepStatus.RUNNING)
        budget_out = (
            instance.remaining_budget_usd is not None and instance.remaining_budget_usd <= 0
        )
        candidates: list[tuple[str, int]] = []

        for step in definition.steps:
            st = instance.step_states[step.step_id]
            deps_done = all(instance.step_states[d].status in _DONE for d in step.dependencies)
            if st.status in (StepStatus.PENDING, StepStatus.READY):
                if deps_done:
                    candidates.append((step.step_id, st.attempts + 1))
            elif st.status == StepStatus.FAILED:
                if st.next_retry_at is None:
                    plan.terminal_failure = plan.terminal_failure or (
                        step.step_id,
                        f"step {step.step_id} failed: {st.error_message}",
                    )
                elif st.next_retry_at <= now:
                    candidates.append((step.step_id, st.attempts + 1))
                else:
                    plan.retry_wakeup = (
                        st.next_retry_at
                        if plan.retry_wakeup is None
                        else min(plan.retry_wakeup, st.next_retry_at)
                    )

        if plan.terminal_failure is not None:
            return plan

        open_esc = {e.step_id for e in instance.escalations if not e.resolved}
        for step_id, _ in candidates:
            step = definition.step(step_id)
            if (
                step.requires_approval
                and step_id not in open_esc
                and not self._approval_granted(instance, step_id)
            ):
                plan.approval_needed.append(step_id)
        if plan.approval_needed:
            return plan

        dispatchable = [c for c in candidates if c[0] not in open_esc]
        if dispatchable and budget_out:
            plan.budget_blocked_step = dispatchable[0][0]
            return plan

        free = max(definition.max_concurrent_steps - running, 0)
        plan.dispatch = dispatchable[:free]

        if not plan.dispatch and plan.retry_wakeup is None and running == 0:
            plan.all_settled = all(
                instance.step_states[s.step_id].status in _SETTLED for s in definition.steps
            )
        return plan

    @staticmethod
    def _approval_granted(instance: WorkflowInstance, step_id: str) -> bool:
        return any(e.step_id == step_id and e.resolved for e in instance.escalations)

    # --- dispatch --------------------------------------------------
    async def _dispatch_wave(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        dispatch: list[tuple[str, int]],
        lease: Lease,
        guard: Guard,
    ) -> WorkflowInstance:
        started: list[BaseEvent] = []
        for sid, att in dispatch:
            if instance.step_states[sid].status == StepStatus.PENDING:
                started.append(
                    self._event(
                        instance,
                        E.StepStatusChanged,
                        step_id=sid,
                        from_status=StepStatus.PENDING,
                        to_status=StepStatus.READY,
                        reason="dependencies satisfied",
                    )
                )
            started.append(
                self._event(
                    instance,
                    E.StepStarted,
                    step_id=sid,
                    attempt=att,
                    worker_id=lease.worker_id,
                )
            )
        instance = await self._append(instance, definition, started, guard)

        budget_view = (
            await self._budget.view(instance, now=self._clock.now())
            if self._budget is not None
            else None
        )

        async def _one(sid: str, att: int) -> tuple[str, int, WorkflowStep, StepOutcome]:
            step = definition.step(sid)
            ctx = StepContext(
                instance_id=instance.instance_id,
                tenant_id=instance.tenant_id,
                step_id=sid,
                agent_type=step.agent_type,
                attempt=att,
                inputs=self._resolve_inputs(instance, step),
                instance_context=dict(instance.context),
                clock=self._clock,
                guard=self._side_effects,
                llm_client=self._llm,
                budget=budget_view,
                replay_llm=list(instance.step_states[sid].recorded_llm_calls),
            )
            started = self._clock.now()
            with span(
                "workflow.step",
                instance_id=instance.instance_id,
                step_id=sid,
                agent_type=step.agent_type,
                attempt=att,
            ):
                outcome = await self._executor.run_attempt(
                    ctx, timeout_seconds=step.timeout_seconds
                )
            elapsed = (self._clock.now() - started).total_seconds()
            metrics.record_step(step.agent_type, "success" if outcome.ok else "failure", elapsed)
            return sid, att, step, outcome

        results = await asyncio.gather(*(_one(sid, att) for sid, att in dispatch))

        drafts: list[BaseEvent] = []
        for sid, att, _step, outcome in results:
            for rec in outcome.llm_recordings:
                drafts.append(
                    self._event(
                        instance,
                        E.LLMCallRecorded,
                        step_id=sid,
                        attempt=att,
                        request_digest=rec["request_digest"],
                        model=rec["model"],
                        response=rec["response"],
                        tokens_input=rec["tokens_input"],
                        tokens_output=rec["tokens_output"],
                        cost_usd=rec["cost_usd"],
                    )
                )
            for c in outcome.charges:
                drafts.append(
                    self._event(
                        instance,
                        E.CostCharged,
                        step_id=sid,
                        amount_usd=c.amount_usd,
                        model=c.model,
                        tokens_input=c.tokens_input,
                        tokens_output=c.tokens_output,
                    )
                )
            for eff in outcome.effects:
                metrics.record_side_effect(
                    eff.effect_name,
                    guarantee=eff.guarantee,
                    outcome="deduplicated" if eff.deduplicated else "executed",
                )
                if not eff.deduplicated:
                    drafts.append(
                        self._event(
                            instance,
                            E.SideEffectIntentRecorded,
                            step_id=sid,
                            effect_name=eff.effect_name,
                            idempotency_key=eff.idempotency_key,
                            params=eff.params,
                            guarantee=eff.guarantee,
                        )
                    )
                drafts.append(
                    self._event(
                        instance,
                        E.SideEffectExecuted,
                        step_id=sid,
                        idempotency_key=eff.idempotency_key,
                        result=eff.result.data,
                        deduplicated=eff.deduplicated,
                    )
                )
            if outcome.ok:
                drafts.append(
                    self._event(
                        instance,
                        E.StepCompleted,
                        step_id=sid,
                        attempt=att,
                        output=outcome.output,
                        model_used=outcome.model_used,
                    )
                )
            else:
                drafts.append(
                    self._event(
                        instance,
                        E.StepFailed,
                        step_id=sid,
                        attempt=att,
                        error_type=outcome.error_type,
                        error_message=outcome.error_message,
                        retryable=outcome.retryable,
                    )
                )
        instance = await self._append(instance, definition, drafts, guard)

        if self._budget is not None:
            spent = sum(c.amount_usd for _, _, _, o in results for c in o.charges)
            if spent > 0:
                await self._budget.record_spend(instance.tenant_id, spent, now=self._clock.now())

        retry_drafts: list[BaseEvent] = []
        now = self._clock.now()
        for sid, att, step, outcome in results:
            if not outcome.ok and outcome.retryable and att <= step.retry_policy.max_retries:
                run_at = now + timedelta(seconds=step.retry_policy.backoff_delay(att))
                retry_drafts.append(
                    self._event(
                        instance,
                        E.StepRetryScheduled,
                        step_id=sid,
                        next_attempt=att + 1,
                        run_at=run_at,
                    )
                )
        if retry_drafts:
            instance = await self._append(instance, definition, retry_drafts, guard)

        reviews: list[tuple[str, ReviewRequest]] = [
            (sid, o.review)
            for sid, _, _, o in results
            if isinstance(o, StepSuccess) and o.review is not None
        ]
        if reviews:
            instance = await self._raise_review_escalations(instance, definition, reviews, guard)
        return instance

    async def _raise_review_escalations(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        reviews: list[tuple[str, ReviewRequest]],
        guard: Guard,
    ) -> WorkflowInstance:
        now = self._clock.now()
        drafts: list[BaseEvent] = []
        for sid, req in reviews:
            deadline = now + timedelta(seconds=req.timeout_seconds) if req.timeout_seconds else None
            drafts.append(
                self._event(
                    instance,
                    E.EscalationRaised,
                    escalation_id=self._ids.new_id(),
                    step_id=sid,
                    reason=req.reason,
                    recommendation=req.recommendation,
                    confidence=req.confidence,
                    options=req.options,
                    deadline=deadline,
                    auto_action=req.auto_action,
                )
            )
            metrics.record_escalation(req.reason)
        drafts.append(
            self._event(
                instance,
                E.InstanceStatusChanged,
                from_status=WorkflowStatus.RUNNING,
                to_status=WorkflowStatus.WAITING_APPROVAL,
                reason=f"review requested: {', '.join(s for s, _ in reviews)}",
            )
        )
        instance = await self._append(instance, definition, drafts, guard)
        reasons = ", ".join(sorted({r.reason for _, r in reviews}))
        step_list = ", ".join(s for s, _ in reviews)
        await self._notify(instance, reasons, f"steps awaiting review: {step_list}")
        return instance

    @staticmethod
    def _resolve_inputs(instance: WorkflowInstance, step: WorkflowStep) -> dict[str, Any]:
        inputs: dict[str, Any] = dict(instance.context)
        for dep in step.dependencies:
            st = instance.step_states.get(dep)
            inputs[dep] = st.output if st and st.output is not None else {}
        return inputs

    # --- terminal transitions ------------------------------------
    async def _complete(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        guard: Guard,
    ) -> DriveReport:
        outputs = {
            sid: st.output
            for sid, st in instance.step_states.items()
            if st.status == StepStatus.COMPLETED and st.output is not None
        }
        drafts = [
            self._event(
                instance,
                E.InstanceStatusChanged,
                from_status=WorkflowStatus.RUNNING,
                to_status=WorkflowStatus.COMPLETED,
            ),
            self._event(instance, E.InstanceCompleted, outputs=outputs),
        ]
        await self._append(instance, definition, drafts, guard)
        return DriveReport(instance.instance_id, DriveResult.COMPLETED)

    async def _apply_failure_policy(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        failure: tuple[str, str],
        guard: Guard,
    ) -> DriveReport:
        step_id, reason = failure

        # a budget refusal is not a workflow bug — surface it for a human to raise
        # the limit and resume, regardless of on_failure policy.
        st = instance.step_states.get(step_id)
        if st is not None and st.error_type == "BudgetExceededError":
            return await self._escalate_cost_threshold(instance, definition, step_id, guard)

        if definition.on_failure == OnFailure.ESCALATE:
            return await self._escalate_max_retries(instance, definition, step_id, reason, guard)

        if definition.on_failure == OnFailure.ROLLBACK:
            return await self._rollback(instance, definition, reason, guard)

        if definition.on_failure == OnFailure.DEAD_LETTER:
            instance = await self._transition(
                instance,
                definition,
                WorkflowStatus.RUNNING,
                WorkflowStatus.DEAD_LETTERED,
                guard,
                reason=reason,
            )
            await self._dead_letters.record(instance, step_id=step_id, reason=reason)
            metrics.record_dead_letter()
            return DriveReport(instance.instance_id, DriveResult.DEAD_LETTERED)

        await self._transition(
            instance,
            definition,
            WorkflowStatus.RUNNING,
            WorkflowStatus.PAUSED,
            guard,
            reason=reason,
        )
        return DriveReport(instance.instance_id, DriveResult.PAUSED)

    async def _escalate_max_retries(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        step_id: str,
        reason: str,
        guard: Guard,
    ) -> DriveReport:
        st = instance.step_states.get(step_id)
        drafts: list[BaseEvent] = [
            self._event(
                instance,
                E.EscalationRaised,
                escalation_id=self._ids.new_id(),
                step_id=step_id,
                reason="max_retries",
                recommendation=(
                    f"step {step_id} exhausted its retries: {st.error_message if st else reason}. "
                    "resolve with retry / skip / abort."
                ),
                auto_action="abort",
            ),
            self._event(
                instance,
                E.InstanceStatusChanged,
                from_status=WorkflowStatus.RUNNING,
                to_status=WorkflowStatus.WAITING_APPROVAL,
                reason=reason,
            ),
        ]
        await self._append(instance, definition, drafts, guard)
        metrics.record_escalation("max_retries")
        await self._notify(instance, "max_retries", reason)
        return DriveReport(instance.instance_id, DriveResult.WAITING_APPROVAL)

    async def _escalate_cost_threshold(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        step_id: str,
        guard: Guard,
    ) -> DriveReport:
        drafts: list[BaseEvent] = [
            self._event(
                instance,
                E.EscalationRaised,
                escalation_id=self._ids.new_id(),
                step_id=step_id,
                reason="cost_threshold",
                recommendation="raise the workflow / tenant budget, then resume",
                auto_action="abort",
            ),
            self._event(
                instance,
                E.InstanceStatusChanged,
                from_status=WorkflowStatus.RUNNING,
                to_status=WorkflowStatus.WAITING_APPROVAL,
                reason="budget exceeded (pre-flight)",
            ),
        ]
        await self._append(instance, definition, drafts, guard)
        metrics.record_escalation("cost_threshold")
        metrics.record_budget_refusal("workflow")
        await self._notify(instance, "cost_threshold", "a step's projected cost exceeds the budget")
        return DriveReport(instance.instance_id, DriveResult.WAITING_APPROVAL)

    async def _rollback(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        reason: str,
        guard: Guard,
    ) -> DriveReport:
        # (1) undo external side effects, newest first
        try:
            undone = (
                await self._side_effects.compensate_instance(
                    instance.instance_id, instance.tenant_id
                )
                if self._side_effects is not None
                else []
            )
        except CompensationError as exc:
            return await self._escalate_compensation_failure(instance, definition, str(exc), guard)

        # (2) run per-step compensation handlers, reverse definition order
        to_compensate = [
            step
            for step in reversed(definition.steps)
            if step.compensation_action
            and instance.step_states[step.step_id].status == StepStatus.COMPLETED
        ]
        for step in to_compensate:
            try:
                await self._run_step_compensation(instance, step)
            except Exception as exc:  # noqa: BLE001 - any handler failure -> escalate
                return await self._escalate_compensation_failure(
                    instance,
                    definition,
                    f"compensation for step {step.step_id} failed: {exc}",
                    guard,
                )

        drafts: list[BaseEvent] = []
        for eff in undone:
            drafts.append(
                self._event(
                    instance,
                    E.SideEffectCompensated,
                    step_id=eff.step_id,
                    idempotency_key=eff.idempotency_key,
                    result=eff.result.data,
                )
            )
        for step in to_compensate:
            drafts.append(
                self._event(
                    instance,
                    E.StepCompensated,
                    step_id=step.step_id,
                    compensation_action=step.compensation_action or "",
                )
            )
        drafts.append(
            self._event(
                instance,
                E.InstanceStatusChanged,
                from_status=WorkflowStatus.RUNNING,
                to_status=WorkflowStatus.ROLLED_BACK,
                reason=reason,
            )
        )
        await self._append(instance, definition, drafts, guard)
        return DriveReport(instance.instance_id, DriveResult.ROLLED_BACK)

    async def _run_step_compensation(self, instance: WorkflowInstance, step: WorkflowStep) -> None:
        ctx = StepContext(
            instance_id=instance.instance_id,
            tenant_id=instance.tenant_id,
            step_id=step.step_id,
            agent_type=step.compensation_action or "",
            attempt=1,
            inputs={"output": instance.step_states[step.step_id].output or {}},
            instance_context=dict(instance.context),
            clock=self._clock,
            guard=self._side_effects,
        )
        outcome = await self._executor.run_attempt(ctx, timeout_seconds=step.timeout_seconds)
        if not outcome.ok:
            raise CompensationError(outcome.error_message)

    async def _escalate_compensation_failure(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        detail: str,
        guard: Guard,
    ) -> DriveReport:
        log.error("compensation_failed", instance_id=instance.instance_id, detail=detail)
        drafts: list[BaseEvent] = [
            self._event(
                instance,
                E.EscalationRaised,
                escalation_id=self._ids.new_id(),
                step_id="",
                reason="compensation_failed",
                recommendation=detail,
                auto_action="abort",
            ),
            self._event(
                instance,
                E.InstanceStatusChanged,
                from_status=WorkflowStatus.RUNNING,
                to_status=WorkflowStatus.WAITING_APPROVAL,
                reason="compensation failed — needs a human",
            ),
        ]
        await self._append(instance, definition, drafts, guard)
        metrics.record_escalation("compensation_failed")
        await self._notify(instance, "compensation_failed", detail)
        return DriveReport(instance.instance_id, DriveResult.WAITING_APPROVAL)

    async def _escalate_for_approval(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        step_ids: list[str],
        guard: Guard,
    ) -> DriveReport:
        now = self._clock.now()
        drafts: list[BaseEvent] = []
        for sid in step_ids:
            step = definition.step(sid)
            if instance.step_states[sid].status == StepStatus.PENDING:
                drafts.append(
                    self._event(
                        instance,
                        E.StepStatusChanged,
                        step_id=sid,
                        from_status=StepStatus.PENDING,
                        to_status=StepStatus.READY,
                        reason="dependencies satisfied",
                    )
                )
            deadline = (
                now + timedelta(seconds=step.approval_timeout_seconds)
                if step.approval_timeout_seconds
                else None
            )
            drafts.append(
                self._event(
                    instance,
                    E.EscalationRaised,
                    escalation_id=self._ids.new_id(),
                    step_id=sid,
                    reason="explicit_approval",
                    deadline=deadline,
                    auto_action=step.approval_auto_action,
                )
            )
            metrics.record_escalation("explicit_approval")
        drafts.append(
            self._event(
                instance,
                E.InstanceStatusChanged,
                from_status=WorkflowStatus.RUNNING,
                to_status=WorkflowStatus.WAITING_APPROVAL,
                reason=f"approval required: {', '.join(step_ids)}",
            )
        )
        await self._append(instance, definition, drafts, guard)
        await self._notify(
            instance, "explicit_approval", f"steps awaiting sign-off: {', '.join(step_ids)}"
        )
        return DriveReport(instance.instance_id, DriveResult.WAITING_APPROVAL)

    async def _block_on_budget(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        step_id: str,
        guard: Guard,
    ) -> DriveReport:
        drafts: list[BaseEvent] = [
            self._event(
                instance,
                E.BudgetExceeded,
                step_id=step_id,
                projected_cost_usd=0.0,
                remaining_budget_usd=instance.remaining_budget_usd or 0.0,
                scope="workflow",
            ),
            self._event(
                instance,
                E.InstanceStatusChanged,
                from_status=WorkflowStatus.RUNNING,
                to_status=WorkflowStatus.PAUSED,
                reason="budget exhausted",
            ),
        ]
        await self._append(instance, definition, drafts, guard)
        metrics.record_budget_refusal("workflow")
        return DriveReport(instance.instance_id, DriveResult.PAUSED)

    # --- recovery --------------------------------------------------
    async def _recover_in_flight(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        guard: Guard,
    ) -> WorkflowInstance:
        stale = [sid for sid, st in instance.step_states.items() if st.status == StepStatus.RUNNING]
        if not stale:
            return instance
        log.info("recovering_in_flight_steps", instance_id=instance.instance_id, steps=stale)
        drafts = [
            self._event(
                instance,
                E.StepStatusChanged,
                step_id=sid,
                from_status=StepStatus.RUNNING,
                to_status=StepStatus.READY,
                reason="worker-recovered",
            )
            for sid in stale
        ]
        return await self._append(instance, definition, drafts, guard)

    # --- append plumbing ----------------------------------------
    def _event(self, instance: WorkflowInstance, cls: type[BaseEvent], **kw: Any) -> BaseEvent:
        return cls(
            event_id=self._ids.new_id(),
            instance_id=instance.instance_id,
            tenant_id=instance.tenant_id,
            sequence=1,  # placeholder; append_new assigns the real sequence
            occurred_at=self._clock.now(),
            **kw,
        )

    async def _append(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        drafts: list[BaseEvent],
        guard: Guard,
        *,
        next_wakeup_at: datetime | None = None,
    ) -> WorkflowInstance:
        if not drafts:
            return instance
        _, events = await self._journal.append_new(
            instance.instance_id,
            instance.tenant_id,
            drafts,
            expected_version=instance.version,
            guard=guard,
            next_wakeup_at=next_wakeup_at,
        )
        return fold(events, base=instance, definition=definition)

    async def _transition(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        frm: WorkflowStatus,
        to: WorkflowStatus,
        guard: Guard,
        *,
        reason: str | None = None,
        next_wakeup_at: datetime | None = None,
    ) -> WorkflowInstance:
        draft = self._event(
            instance,
            E.InstanceStatusChanged,
            from_status=frm,
            to_status=to,
            reason=reason,
        )
        return await self._append(
            instance, definition, [draft], guard, next_wakeup_at=next_wakeup_at
        )
