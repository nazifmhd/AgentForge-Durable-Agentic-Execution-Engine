"""Runtime projection models — rebuilt by :func:`agentforge.core.events.fold.fold`.

``WorkflowInstance`` is *derived state*: never mutate it directly outside the
fold. Read-only helpers here are for the scheduler / API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.domain.enums import (
    TERMINAL_WORKFLOW_STATUSES,
    StepStatus,
    WorkflowStatus,
)


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output


class StepState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    output: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    model_used: str | None = None
    cost_usd: float = 0.0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    next_retry_at: datetime | None = None


class ErrorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str | None = None
    error_type: str
    error_message: str
    occurred_at: datetime
    attempt: int | None = None


class EscalationRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    escalation_id: str
    step_id: str
    reason: str
    deadline: datetime | None = None
    resolved: bool = False


class WorkflowInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str
    tenant_id: str
    workflow_id: str
    workflow_version: str
    status: WorkflowStatus = WorkflowStatus.PENDING

    context: dict[str, Any] = Field(default_factory=dict)
    step_states: dict[str, StepState] = Field(default_factory=dict)

    cost_accumulated_usd: float = 0.0
    tokens_used: TokenUsage = Field(default_factory=TokenUsage)
    budget_limit_usd: float | None = None

    error_history: list[ErrorRecord] = Field(default_factory=list)
    escalations: list[EscalationRef] = Field(default_factory=list)

    # Version == sequence number of the last event folded in. This is the value
    # a writer passes to EventStore.append as ``expected_version``.
    version: int = 0

    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    # --- derived read helpers -------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_WORKFLOW_STATUSES

    @property
    def remaining_budget_usd(self) -> float | None:
        if self.budget_limit_usd is None:
            return None
        return self.budget_limit_usd - self.cost_accumulated_usd

    def step(self, step_id: str) -> StepState:
        return self.step_states[step_id]

    def _step_done(self, step_id: str) -> bool:
        st = self.step_states.get(step_id)
        return st is not None and st.status in (
            StepStatus.COMPLETED,
            StepStatus.SKIPPED,
        )

    def ready_steps(self, definition: WorkflowDefinition) -> list[str]:
        """Steps whose dependencies are all satisfied and which have not started."""
        out: list[str] = []
        for step in definition.steps:
            st = self.step_states.get(step.step_id)
            if st is not None and st.status not in (
                StepStatus.PENDING,
                StepStatus.READY,
            ):
                continue
            if all(self._step_done(dep) for dep in step.dependencies):
                out.append(step.step_id)
        return out

    def all_steps_settled(self, definition: WorkflowDefinition) -> bool:
        settled = {
            StepStatus.COMPLETED,
            StepStatus.SKIPPED,
            StepStatus.COMPENSATED,
        }
        return all(
            self.step_states.get(s.step_id, StepState(step_id=s.step_id)).status in settled
            for s in definition.steps
        )
