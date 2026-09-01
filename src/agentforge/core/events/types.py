"""The event vocabulary.

Every state change in the engine is one of these. Events are immutable, carry a
per-instance monotonic ``sequence``, and serialize to a single JSONB ``payload``
column (envelope fields included) so the row is self-describing.

Discriminated on ``event_type`` — :data:`EventAdapter` parses a stored dict back
into the right subclass.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agentforge.core.domain.enums import StepStatus, WorkflowStatus


class BaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    instance_id: str
    tenant_id: str
    sequence: int = Field(ge=1)
    occurred_at: datetime
    event_type: str

    # convenience for step-scoped events
    @property
    def key(self) -> tuple[str, int]:
        return (self.instance_id, self.sequence)


# --- lifecycle -----------------------------------------------------------
class InstanceCreated(BaseEvent):
    event_type: Literal["InstanceCreated"] = "InstanceCreated"
    workflow_id: str
    workflow_version: str
    context: dict[str, Any] = Field(default_factory=dict)
    budget_limit_usd: float | None = None
    trigger_source: str = "api"
    trigger_metadata: dict[str, Any] = Field(default_factory=dict)


class InstanceStatusChanged(BaseEvent):
    event_type: Literal["InstanceStatusChanged"] = "InstanceStatusChanged"
    from_status: WorkflowStatus
    to_status: WorkflowStatus
    reason: str | None = None


class InstanceCompleted(BaseEvent):
    event_type: Literal["InstanceCompleted"] = "InstanceCompleted"
    outputs: dict[str, Any] = Field(default_factory=dict)


class InstanceFailed(BaseEvent):
    event_type: Literal["InstanceFailed"] = "InstanceFailed"
    error_type: str
    error_message: str


# --- step execution ----------------------------------------------------
class StepStatusChanged(BaseEvent):
    """Generic step transition (PENDING->READY, etc.) not covered by a richer event."""

    event_type: Literal["StepStatusChanged"] = "StepStatusChanged"
    step_id: str
    from_status: StepStatus
    to_status: StepStatus
    reason: str | None = None


class StepStarted(BaseEvent):
    event_type: Literal["StepStarted"] = "StepStarted"
    step_id: str
    attempt: int = Field(ge=1)
    worker_id: str
    model_selected: str | None = None


class StepCompleted(BaseEvent):
    event_type: Literal["StepCompleted"] = "StepCompleted"
    step_id: str
    attempt: int = Field(ge=1)
    output: dict[str, Any] = Field(default_factory=dict)
    model_used: str | None = None
    cost_usd: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    duration_ms: int = 0


class StepFailed(BaseEvent):
    event_type: Literal["StepFailed"] = "StepFailed"
    step_id: str
    attempt: int = Field(ge=1)
    error_type: str
    error_message: str
    retryable: bool = False


class StepRetryScheduled(BaseEvent):
    event_type: Literal["StepRetryScheduled"] = "StepRetryScheduled"
    step_id: str
    next_attempt: int = Field(ge=2)
    run_at: datetime


class StepSkipped(BaseEvent):
    event_type: Literal["StepSkipped"] = "StepSkipped"
    step_id: str
    reason: str


class StepCompensated(BaseEvent):
    event_type: Literal["StepCompensated"] = "StepCompensated"
    step_id: str
    compensation_action: str


# --- deterministic-replay inputs (ADR-0005) ---------------------------
class LLMCallRecorded(BaseEvent):
    event_type: Literal["LLMCallRecorded"] = "LLMCallRecorded"
    step_id: str
    attempt: int = Field(ge=1)
    request_digest: str
    model: str
    response: dict[str, Any]
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0


class ToolCallRecorded(BaseEvent):
    event_type: Literal["ToolCallRecorded"] = "ToolCallRecorded"
    step_id: str
    attempt: int = Field(ge=1)
    tool_name: str
    args_digest: str
    result: Any


# --- side effects (ADR-0003) ----------------------------------------
class SideEffectIntentRecorded(BaseEvent):
    event_type: Literal["SideEffectIntentRecorded"] = "SideEffectIntentRecorded"
    step_id: str
    effect_name: str
    idempotency_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    guarantee: Literal["exactly_once", "at_least_once_dedup"] = "at_least_once_dedup"


class SideEffectExecuted(BaseEvent):
    event_type: Literal["SideEffectExecuted"] = "SideEffectExecuted"
    step_id: str
    idempotency_key: str
    result: dict[str, Any] = Field(default_factory=dict)
    deduplicated: bool = False


class SideEffectCompensated(BaseEvent):
    event_type: Literal["SideEffectCompensated"] = "SideEffectCompensated"
    step_id: str
    idempotency_key: str
    result: dict[str, Any] = Field(default_factory=dict)


# --- human-in-the-loop ----------------------------------------------
class EscalationRaised(BaseEvent):
    event_type: Literal["EscalationRaised"] = "EscalationRaised"
    escalation_id: str
    step_id: str
    reason: str
    recommendation: str = ""
    confidence: float = 0.0
    options: list[dict[str, Any]] = Field(default_factory=list)
    deadline: datetime | None = None
    auto_action: str = "abort"


class EscalationResolved(BaseEvent):
    event_type: Literal["EscalationResolved"] = "EscalationResolved"
    escalation_id: str
    step_id: str
    resolution: str  # approve | modify | abort | skip
    resolved_by: str
    modified_context: dict[str, Any] | None = None


class EscalationTimedOut(BaseEvent):
    event_type: Literal["EscalationTimedOut"] = "EscalationTimedOut"
    escalation_id: str
    step_id: str
    auto_action: str


# --- cost / budget --------------------------------------------------
class CostCharged(BaseEvent):
    event_type: Literal["CostCharged"] = "CostCharged"
    step_id: str | None = None
    amount_usd: float
    model: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0


class BudgetExceeded(BaseEvent):
    event_type: Literal["BudgetExceeded"] = "BudgetExceeded"
    step_id: str
    projected_cost_usd: float
    remaining_budget_usd: float
    scope: Literal["workflow", "tenant_daily"] = "workflow"


AnyEvent = Annotated[
    InstanceCreated
    | InstanceStatusChanged
    | InstanceCompleted
    | InstanceFailed
    | StepStatusChanged
    | StepStarted
    | StepCompleted
    | StepFailed
    | StepRetryScheduled
    | StepSkipped
    | StepCompensated
    | LLMCallRecorded
    | ToolCallRecorded
    | SideEffectIntentRecorded
    | SideEffectExecuted
    | SideEffectCompensated
    | EscalationRaised
    | EscalationResolved
    | EscalationTimedOut
    | CostCharged
    | BudgetExceeded,
    Field(discriminator="event_type"),
]

EventAdapter: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


def parse_event(data: dict[str, Any]) -> AnyEvent:
    return EventAdapter.validate_python(data)


def dump_event(event: BaseEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


EVENT_TYPES: dict[str, type[BaseEvent]] = {
    str(cls.model_fields["event_type"].default): cls
    for cls in (
        InstanceCreated,
        InstanceStatusChanged,
        InstanceCompleted,
        InstanceFailed,
        StepStatusChanged,
        StepStarted,
        StepCompleted,
        StepFailed,
        StepRetryScheduled,
        StepSkipped,
        StepCompensated,
        LLMCallRecorded,
        ToolCallRecorded,
        SideEffectIntentRecorded,
        SideEffectExecuted,
        SideEffectCompensated,
        EscalationRaised,
        EscalationResolved,
        EscalationTimedOut,
        CostCharged,
        BudgetExceeded,
    )
}
