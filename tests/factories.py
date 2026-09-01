"""Builders for domain objects and event streams used across tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agentforge.core.domain.definition import (
    RetryPolicy,
    WorkflowDefinition,
    WorkflowStep,
)
from agentforge.core.events import types as E

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make_step(step_id: str, deps: tuple[str, ...] = (), **kw: Any) -> WorkflowStep:
    return WorkflowStep(
        step_id=step_id,
        name=kw.pop("name", step_id.replace("_", " ").title()),
        agent_type=kw.pop("agent_type", "executor_agent"),
        dependencies=deps,
        retry_policy=kw.pop("retry_policy", RetryPolicy(max_retries=2)),
        **kw,
    )


def linear_workflow(n: int = 3, **kw: Any) -> WorkflowDefinition:
    steps = [make_step(f"step_{i}", () if i == 1 else (f"step_{i - 1}",)) for i in range(1, n + 1)]
    return WorkflowDefinition(
        workflow_id=kw.pop("workflow_id", "wf-linear"),
        name=kw.pop("name", "Linear"),
        version=kw.pop("version", "1.0.0"),
        steps=tuple(steps),
        **kw,
    )


def diamond_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="wf-diamond",
        name="Diamond",
        version="1.0.0",
        steps=(
            make_step("a"),
            make_step("b", ("a",)),
            make_step("c", ("a",)),
            make_step("d", ("b", "c")),
        ),
    )


class StreamBuilder:
    """Fluent per-instance event builder with automatic sequencing."""

    def __init__(
        self,
        instance_id: str = "inst-1",
        tenant_id: str = "tenant-1",
        clock: datetime = T0,
        start_sequence: int = 0,
    ) -> None:
        self.instance_id = instance_id
        self.tenant_id = tenant_id
        self._seq = start_sequence
        self._now = clock
        self.events: list[E.BaseEvent] = []

    def _next(self, cls: type[E.BaseEvent], **kw: Any) -> E.BaseEvent:
        self._seq += 1
        ev = cls(
            event_id=f"ev-{self._seq}",
            instance_id=self.instance_id,
            tenant_id=self.tenant_id,
            sequence=self._seq,
            occurred_at=self._now,
            **kw,
        )
        self.events.append(ev)
        return ev

    def created(self, **kw: Any) -> StreamBuilder:
        kw.setdefault("workflow_id", "wf-linear")
        kw.setdefault("workflow_version", "1.0.0")
        self._next(E.InstanceCreated, **kw)
        return self

    def wf_status(self, frm: str, to: str, **kw: Any) -> StreamBuilder:
        self._next(E.InstanceStatusChanged, from_status=frm, to_status=to, **kw)
        return self

    def step_status(self, step_id: str, frm: str, to: str) -> StreamBuilder:
        self._next(E.StepStatusChanged, step_id=step_id, from_status=frm, to_status=to)
        return self

    def step_started(self, step_id: str, attempt: int = 1, **kw: Any) -> StreamBuilder:
        kw.setdefault("worker_id", "worker-1")
        self._next(E.StepStarted, step_id=step_id, attempt=attempt, **kw)
        return self

    def step_completed(self, step_id: str, attempt: int = 1, **kw: Any) -> StreamBuilder:
        self._next(E.StepCompleted, step_id=step_id, attempt=attempt, **kw)
        return self

    def step_failed(self, step_id: str, attempt: int = 1, **kw: Any) -> StreamBuilder:
        kw.setdefault("error_type", "LLMTimeoutError")
        kw.setdefault("error_message", "boom")
        kw.setdefault("retryable", True)
        self._next(E.StepFailed, step_id=step_id, attempt=attempt, **kw)
        return self

    def cost(self, amount: float, step_id: str | None = None, **kw: Any) -> StreamBuilder:
        self._next(E.CostCharged, amount_usd=amount, step_id=step_id, **kw)
        return self

    def raw(self, cls: type[E.BaseEvent], **kw: Any) -> StreamBuilder:
        self._next(cls, **kw)
        return self

    def tick(self, seconds: float) -> StreamBuilder:
        self._now = self._now + timedelta(seconds=seconds)
        return self
