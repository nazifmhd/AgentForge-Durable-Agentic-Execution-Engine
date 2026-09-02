"""Workflow definition models — the immutable template an instance executes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.core.domain.dag import topological_layers, validate_dependencies
from agentforge.core.domain.enums import CostTier, OnFailure
from agentforge.core.hashing import digest
from agentforge.exceptions import WorkflowDefinitionError

_DEFAULT_RETRYABLE = ("LLMTimeoutError", "RateLimitError", "MalformedOutputError")


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_retries: int = Field(default=3, ge=0, le=20)
    backoff_base_seconds: float = Field(default=1.0, gt=0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    backoff_max_seconds: float = Field(default=60.0, gt=0)
    jitter: float = Field(default=0.2, ge=0, le=1, description="fraction of ± jitter")
    retry_on: tuple[str, ...] = _DEFAULT_RETRYABLE
    fallback_model: str | None = None

    def backoff_delay(self, attempt: int) -> float:
        """Deterministic (jitter-free) backoff for attempt N (1-based)."""
        raw = self.backoff_base_seconds * (self.backoff_multiplier ** max(attempt - 1, 0))
        return min(raw, self.backoff_max_seconds)


class WorkflowStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    name: str = Field(max_length=256)
    agent_type: str = Field(max_length=128)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: int = Field(default=300, gt=0, le=86_400)
    cost_tier: CostTier = CostTier.STANDARD
    side_effects: tuple[str, ...] = ()
    compensation_action: str | None = None
    requires_approval: bool = False
    # If an approval isn't given within this window, the auto-action fires.
    # None = wait for a human indefinitely.
    approval_timeout_seconds: int | None = Field(default=None, gt=0)
    approval_auto_action: str = Field(default="abort", pattern="^(approve|skip|abort)$")
    dependencies: tuple[str, ...] = ()


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=64)
    name: str = Field(max_length=256)
    description: str = ""
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    steps: tuple[WorkflowStep, ...] = Field(min_length=1)
    global_timeout_seconds: int = Field(default=3600, gt=0)
    max_concurrent_steps: int = Field(default=5, ge=1, le=100)
    on_failure: OnFailure = OnFailure.PAUSE

    @model_validator(mode="after")
    def _validate_graph(self) -> WorkflowDefinition:
        ids = [s.step_id for s in self.steps]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise WorkflowDefinitionError(f"duplicate step ids: {sorted(dupes)}")
        deps = {s.step_id: list(s.dependencies) for s in self.steps}
        validate_dependencies(deps)
        for step in self.steps:
            if step.compensation_action and not step.side_effects:
                raise WorkflowDefinitionError(
                    f"step {step.step_id!r} has a compensation_action but no side_effects"
                )
        return self

    # --- lookups -----------------------------------------------------------
    def step(self, step_id: str) -> WorkflowStep:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        raise KeyError(step_id)

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps)

    def dependents(self, step_id: str) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps if step_id in s.dependencies)

    def execution_layers(self) -> list[list[str]]:
        return topological_layers({s.step_id: list(s.dependencies) for s in self.steps})

    @property
    def checksum(self) -> str:
        """Content hash — identical definitions produce identical checksums."""
        return digest(self.model_dump(mode="json"))
