"""Request / response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ExecuteRequest(BaseModel):
    context: dict[str, Any] = Field(default_factory=dict)
    version: str | None = None  # None -> active version
    budget_limit_usd: float | None = Field(default=None, gt=0)
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    idempotency_key: str | None = None


class ExecuteResponse(BaseModel):
    instance_id: str
    workflow_id: str
    version: str
    status: str


class StepView(BaseModel):
    step_id: str
    status: str
    attempts: int
    model_used: str | None
    cost_usd: float
    error_type: str | None
    error_message: str | None


class InstanceView(BaseModel):
    instance_id: str
    tenant_id: str
    workflow_id: str
    workflow_version: str
    status: str
    version: int
    context: dict[str, Any]
    steps: list[StepView]
    cost_accumulated_usd: float
    budget_limit_usd: float | None
    escalations: list[dict[str, Any]]
    side_effects: list[dict[str, Any]]
    created_at: datetime | None
    updated_at: datetime | None
    completed_at: datetime | None


class InstanceSummaryView(BaseModel):
    instance_id: str
    workflow_id: str
    workflow_version: str
    status: str
    cost_accumulated_usd: float
    budget_limit_usd: float | None
    next_wakeup_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    completed_at: datetime | None


class EventView(BaseModel):
    sequence: int
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]


class ControlRequest(BaseModel):
    actor: str = "operator"


class EscalationView(BaseModel):
    escalation_id: str
    instance_id: str
    step_id: str
    reason: str
    recommendation: str
    confidence: float
    options: list[dict[str, Any]]
    auto_action: str
    deadline: datetime | None
    created_at: datetime


class ResolveRequest(BaseModel):
    resolution: Literal["approve", "modify", "skip", "abort"]
    resolved_by: str
    modified_context: dict[str, Any] | None = None
    new_budget_usd: float | None = Field(default=None, gt=0)


class DeadLetterView(BaseModel):
    id: int
    instance_id: str
    step_id: str | None
    reason: str
    error_type: str | None
    error_message: str | None
    at_version: int
    created_at: datetime


class RequeueResponse(BaseModel):
    instance_id: str
    status: str
