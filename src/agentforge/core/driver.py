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

from agentforge.core.dead_letter import DeadLetterService
from agentforge.core.domain.definition import WorkflowDefinition, WorkflowStep
from agentforge.core.domain.enums import OnFailure, StepStatus, WorkflowStatus
from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.events import BaseEvent, fold
from agentforge.core.events import types as E
from agentforge.core.executor import StepExecutor, StepOutcome
from agentforge.core.leasing import Guard, Lease
from agentforge.core.persistence.protocols import DefinitionSource, EventJournal
from agentforge.core.ports import SYSTEM_CLOCK, UUID_GENERATOR, Clock, IdGenerator
from agentforge.core.runners import StepContext
from agentforge.exceptions import ConfigurationError, ConflictError, LeaseLostError
from agentforge.logging import get_logger

log = get_logger("driver")

_DONE = (StepStatus.COMPLETED, StepStatus.SKIPPED)
_SETTLED = (StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.COMPENSATED)
_MAX_DRIVE_ITERATIONS = 1000


class DriveResult(StrEnum):
    COMPLETED = "completed"
    PAUSED = "paused"
    DEAD_LETTERED = "dead_lettered"
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
        clock: Clock = SYSTEM_CLOCK,
        ids: IdGenerator = UUID_GENERATOR,
    ) -> None:
        self._journal = journal
        self._definitions = definitions
        self._executor = executor
        self._dead_letters = dead_letters
        self._clock = clock
        self._ids = ids

    async def drive(self, lease: Lease, guard: Guard) -> DriveReport:
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
            if status in (WorkflowStatus.COMPLETED, WorkflowStatus.ROLLED_BACK):
                return DriveReport(instance.instance_id, DriveResult.COMPLETED)
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
            )
            outcome = await self._executor.run_attempt(ctx, timeout_seconds=step.timeout_seconds)
            return sid, att, step, outcome

        results = await asyncio.gather(*(_one(sid, att) for sid, att in dispatch))

        drafts: list[BaseEvent] = []
        for sid, att, _step, outcome in results:
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
        if definition.on_failure in (OnFailure.DEAD_LETTER, OnFailure.ROLLBACK):
            if definition.on_failure == OnFailure.ROLLBACK:
                log.warning(
                    "rollback_not_implemented",
                    instance_id=instance.instance_id,
                    note="Phase 3; dead-lettering instead",
                )
            instance = await self._transition(
                instance,
                definition,
                WorkflowStatus.RUNNING,
                WorkflowStatus.DEAD_LETTERED,
                guard,
                reason=reason,
            )
            await self._dead_letters.record(instance, step_id=step_id, reason=reason)
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

    async def _escalate_for_approval(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        step_ids: list[str],
        guard: Guard,
    ) -> DriveReport:
        drafts: list[BaseEvent] = []
        for sid in step_ids:
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
            drafts.append(
                self._event(
                    instance,
                    E.EscalationRaised,
                    escalation_id=self._ids.new_id(),
                    step_id=sid,
                    reason="explicit_approval",
                    auto_action="abort",
                )
            )
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
