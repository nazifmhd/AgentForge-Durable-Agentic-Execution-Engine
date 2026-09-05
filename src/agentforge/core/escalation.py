"""Human-in-the-loop: resolving and auto-expiring escalations.

The driver *raises* escalations (approval gates, cost thresholds, compensation
failures) as events; this module *resolves* them — from an operator action or,
when a deadline lapses, from the configured auto-action — by appending the
resolution events and the workflow/step transitions that unblock the instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge.core.domain.enums import StepStatus, WorkflowStatus
from agentforge.core.events import BaseEvent
from agentforge.core.events import types as E
from agentforge.core.persistence.protocols import EventJournal
from agentforge.core.persistence.tables import EscalationRow
from agentforge.core.ports import SYSTEM_CLOCK, UUID_GENERATOR, Clock, IdGenerator
from agentforge.exceptions import ConfigurationError
from agentforge.integrations.notifications import Notification, Notifier
from agentforge.logging import get_logger

log = get_logger("escalation")

Resolution = Literal["approve", "modify", "skip", "abort"]


class EscalationReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    COST_THRESHOLD = "cost_threshold"
    SENSITIVE_ACTION = "sensitive_action"
    MAX_RETRIES = "max_retries"
    EXPLICIT_APPROVAL = "explicit_approval"
    ANOMALY_DETECTED = "anomaly_detected"
    COMPENSATION_FAILED = "compensation_failed"


@dataclass(frozen=True, slots=True)
class PendingEscalation:
    escalation_id: str
    instance_id: str
    tenant_id: str
    step_id: str
    reason: str
    recommendation: str
    confidence: float
    options: list[dict[str, Any]]
    auto_action: str
    deadline: datetime | None
    status: str
    created_at: datetime


def _row_to_pending(r: Any) -> PendingEscalation:
    return PendingEscalation(
        escalation_id=r.escalation_id,
        instance_id=r.instance_id,
        tenant_id=r.tenant_id,
        step_id=r.step_id,
        reason=r.reason,
        recommendation=r.recommendation,
        confidence=r.confidence,
        options=list(r.options),
        auto_action=r.auto_action,
        deadline=r.deadline,
        status=r.status,
        created_at=r.created_at,
    )


class EscalationReadStore(Protocol):
    async def get(self, escalation_id: str) -> PendingEscalation | None: ...
    async def list_pending(self, tenant_id: str, limit: int) -> list[PendingEscalation]: ...
    async def due(self, now: datetime, limit: int) -> list[PendingEscalation]: ...


class PgEscalationReadStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def get(self, escalation_id: str) -> PendingEscalation | None:
        async with self._sm() as session:
            row = await session.get(EscalationRow, escalation_id)
        return _row_to_pending(row) if row is not None else None

    async def list_pending(self, tenant_id: str, limit: int) -> list[PendingEscalation]:
        async with self._sm() as session:
            rows = await session.scalars(
                select(EscalationRow)
                .where(
                    EscalationRow.tenant_id == tenant_id,
                    EscalationRow.status == "pending",
                )
                .order_by(EscalationRow.created_at)
                .limit(limit)
            )
            return [_row_to_pending(r) for r in rows]

    async def due(self, now: datetime, limit: int) -> list[PendingEscalation]:
        async with self._sm() as session:
            rows = await session.scalars(
                select(EscalationRow)
                .where(
                    EscalationRow.status == "pending",
                    EscalationRow.deadline.is_not(None),
                    EscalationRow.deadline <= now,
                )
                .order_by(EscalationRow.deadline)
                .limit(limit)
            )
            return [_row_to_pending(r) for r in rows]


class EscalationController:
    def __init__(
        self,
        store: EscalationReadStore,
        journal: EventJournal,
        *,
        notifier: Notifier | None = None,
        clock: Clock = SYSTEM_CLOCK,
        ids: IdGenerator = UUID_GENERATOR,
    ) -> None:
        self._store = store
        self._journal = journal
        self._notifier = notifier
        self._clock = clock
        self._ids = ids

    # --- reads --------------------------------------------------------
    async def list_pending(self, *, tenant_id: str, limit: int = 100) -> list[PendingEscalation]:
        return await self._store.list_pending(tenant_id, limit)

    async def get(self, escalation_id: str, *, tenant_id: str) -> PendingEscalation | None:
        esc = await self._store.get(escalation_id)
        if esc is None or esc.tenant_id != tenant_id:
            return None
        return esc

    async def notify_raised(self, escalation_id: str, *, tenant_id: str) -> None:
        if self._notifier is None:
            return
        esc = await self.get(escalation_id, tenant_id=tenant_id)
        if esc is None:
            return
        await self._notifier.notify(
            Notification(
                channel="escalations",
                subject=f"Approval needed: {esc.reason} on {esc.instance_id}",
                body=esc.recommendation or "A workflow step is waiting for a human.",
                metadata={
                    "instance_id": esc.instance_id,
                    "escalation_id": esc.escalation_id,
                    "step_id": esc.step_id,
                    "deadline": esc.deadline.isoformat() if esc.deadline else None,
                },
            )
        )

    # --- writes ------------------------------------------------------
    async def resolve(
        self,
        escalation_id: str,
        *,
        tenant_id: str,
        resolution: Resolution,
        resolved_by: str,
        modified_context: dict[str, Any] | None = None,
        new_budget_usd: float | None = None,
    ) -> str:
        esc, instance = await self._load(escalation_id, tenant_id)
        now = self._clock.now()

        drafts: list[BaseEvent] = [
            self._event(
                E.EscalationResolved,
                instance,
                now,
                escalation_id=escalation_id,
                step_id=esc.step_id,
                resolution=resolution,
                resolved_by=resolved_by,
                modified_context=modified_context,
            )
        ]
        if new_budget_usd is not None:
            drafts.append(
                self._event(
                    E.WorkflowBudgetAdjusted,
                    instance,
                    now,
                    new_limit_usd=new_budget_usd,
                    adjusted_by=resolved_by,
                    reason=f"escalation {escalation_id} resolved",
                )
            )
        drafts.extend(self._transition_events(instance, now, esc.step_id, resolution, resolved_by))

        await self._journal.append_new(
            esc.instance_id, tenant_id, drafts, expected_version=instance.version
        )
        log.info(
            "escalation_resolved",
            escalation_id=escalation_id,
            resolution=resolution,
            by=resolved_by,
        )
        return esc.instance_id

    async def expire_due(self, now: datetime, *, limit: int = 50) -> list[str]:
        rows = await self._store.due(now, limit)
        fired: list[str] = []
        for row in rows:
            instance = await self._journal.get_instance(row.instance_id, row.tenant_id)
            if instance is None or instance.status != WorkflowStatus.WAITING_APPROVAL:
                continue
            action = row.auto_action if row.auto_action in ("approve", "skip", "abort") else "abort"
            drafts: list[BaseEvent] = [
                self._event(
                    E.EscalationTimedOut,
                    instance,
                    now,
                    escalation_id=row.escalation_id,
                    step_id=row.step_id,
                    auto_action=action,
                )
            ]
            drafts.extend(self._transition_events(instance, now, row.step_id, action, "auto"))
            try:
                await self._journal.append_new(
                    row.instance_id,
                    row.tenant_id,
                    drafts,
                    expected_version=instance.version,
                )
                fired.append(row.escalation_id)
                log.info(
                    "escalation_timed_out",
                    escalation_id=row.escalation_id,
                    auto_action=action,
                )
            except Exception:
                log.exception("escalation_timeout_failed", escalation_id=row.escalation_id)
        return fired

    # --- helpers ----------------------------------------------------
    async def _load(self, escalation_id: str, tenant_id: str) -> tuple[PendingEscalation, Any]:
        esc = await self.get(escalation_id, tenant_id=tenant_id)
        if esc is None:
            raise ConfigurationError(f"escalation {escalation_id} not found")
        if esc.status != "pending":
            raise ConfigurationError(f"escalation {escalation_id} is {esc.status}")
        instance = await self._journal.get_instance(esc.instance_id, tenant_id)
        if instance is None:
            raise ConfigurationError(f"instance {esc.instance_id} not found")
        if instance.status != WorkflowStatus.WAITING_APPROVAL:
            raise ConfigurationError(
                f"instance {esc.instance_id} is {instance.status.value}, not waiting_approval"
            )
        return esc, instance

    def _transition_events(
        self,
        instance: Any,
        now: datetime,
        step_id: str,
        resolution: str,
        actor: str,
    ) -> list[BaseEvent]:
        out: list[BaseEvent] = []
        if step_id and step_id in instance.step_states:
            st = instance.step_states[step_id]
            if resolution in ("approve", "modify"):
                # A step that already ran and asked for review keeps its output;
                # one that was gated *before* running goes back to the queue.
                already_ran = st.output is not None
                to_status = StepStatus.COMPLETED if already_ran else StepStatus.READY
                out.append(
                    self._event(
                        E.StepStatusChanged,
                        instance,
                        now,
                        step_id=step_id,
                        from_status=st.status,
                        to_status=to_status,
                        reason=f"approved by {actor}",
                    )
                )
                out.append(self._wf(instance, now, WorkflowStatus.RUNNING, f"resumed by {actor}"))
            elif resolution == "skip":
                out.append(
                    self._event(
                        E.StepStatusChanged,
                        instance,
                        now,
                        step_id=step_id,
                        from_status=st.status,
                        to_status=StepStatus.SKIPPED,
                        reason=f"skipped by {actor}",
                    )
                )
                out.append(self._wf(instance, now, WorkflowStatus.RUNNING, f"resumed by {actor}"))
            else:  # abort
                out.append(self._wf(instance, now, WorkflowStatus.FAILED, f"aborted by {actor}"))
        else:  # workflow-level escalation (e.g. compensation failure)
            target = (
                WorkflowStatus.ROLLED_BACK
                if resolution in ("approve", "modify", "skip")
                else WorkflowStatus.FAILED
            )
            out.append(self._wf(instance, now, target, f"{resolution} by {actor}"))
        return out

    def _wf(self, instance: Any, now: datetime, to: WorkflowStatus, reason: str) -> BaseEvent:
        return self._event(
            E.InstanceStatusChanged,
            instance,
            now,
            from_status=instance.status,
            to_status=to,
            reason=reason,
        )

    def _event(self, cls: type[BaseEvent], instance: Any, now: datetime, **kw: Any) -> BaseEvent:
        return cls(
            event_id=self._ids.new_id(),
            instance_id=instance.instance_id,
            tenant_id=instance.tenant_id,
            sequence=1,  # placeholder; append_new assigns the real sequence
            occurred_at=now,
            **kw,
        )
