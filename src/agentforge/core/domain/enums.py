"""State vocabularies and the transition tables the fold enforces."""

from __future__ import annotations

from enum import StrEnum


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"  # explicit human pause
    WAITING_APPROVAL = "waiting_approval"  # blocked on an escalation
    RETRYING = "retrying"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"  # retries exhausted; replayable by an operator
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


class StepStatus(StrEnum):
    PENDING = "pending"  # dependencies not yet satisfied
    READY = "ready"  # dependencies satisfied, awaiting a worker
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPENSATED = "compensated"  # its side effects were rolled back


class CostTier(StrEnum):
    CHEAP = "cheap"
    STANDARD = "standard"
    PREMIUM = "premium"
    CRITICAL = "critical"


class OnFailure(StrEnum):
    PAUSE = "pause"
    ROLLBACK = "rollback"
    DEAD_LETTER = "dead_letter"


class TriggerSource(StrEnum):
    API = "api"
    N8N = "n8n"
    SCHEDULER = "scheduler"
    MANUAL = "manual"
    REPLAY = "replay"


TERMINAL_WORKFLOW_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {WorkflowStatus.COMPLETED, WorkflowStatus.ROLLED_BACK}
)

# Allowed workflow status transitions. DEAD_LETTERED -> RUNNING covers operator replay.
WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.PENDING: frozenset(
        {WorkflowStatus.RUNNING, WorkflowStatus.FAILED, WorkflowStatus.ROLLED_BACK}
    ),
    WorkflowStatus.RUNNING: frozenset(
        {
            WorkflowStatus.PAUSED,
            WorkflowStatus.WAITING_APPROVAL,
            WorkflowStatus.RETRYING,
            WorkflowStatus.FAILED,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.DEAD_LETTERED,
            WorkflowStatus.ROLLED_BACK,
        }
    ),
    WorkflowStatus.RETRYING: frozenset(
        {WorkflowStatus.RUNNING, WorkflowStatus.FAILED, WorkflowStatus.DEAD_LETTERED}
    ),
    WorkflowStatus.PAUSED: frozenset(
        {WorkflowStatus.RUNNING, WorkflowStatus.ROLLED_BACK, WorkflowStatus.FAILED}
    ),
    WorkflowStatus.WAITING_APPROVAL: frozenset(
        {
            WorkflowStatus.RUNNING,
            WorkflowStatus.ROLLED_BACK,
            WorkflowStatus.FAILED,
            WorkflowStatus.DEAD_LETTERED,
        }
    ),
    WorkflowStatus.FAILED: frozenset(
        {
            WorkflowStatus.RUNNING,
            WorkflowStatus.RETRYING,
            WorkflowStatus.ROLLED_BACK,
            WorkflowStatus.DEAD_LETTERED,
        }
    ),
    WorkflowStatus.DEAD_LETTERED: frozenset({WorkflowStatus.RUNNING}),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.ROLLED_BACK: frozenset(),
}

STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.READY, StepStatus.SKIPPED}),
    StepStatus.READY: frozenset(
        {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.WAITING_APPROVAL}
    ),
    StepStatus.RUNNING: frozenset(
        {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.WAITING_APPROVAL}
    ),
    StepStatus.WAITING_APPROVAL: frozenset(
        {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.FAILED}
    ),
    StepStatus.FAILED: frozenset(
        {StepStatus.READY, StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.COMPENSATED}
    ),
    StepStatus.COMPLETED: frozenset({StepStatus.COMPENSATED}),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.COMPENSATED: frozenset(),
}


def workflow_transition_allowed(src: WorkflowStatus, dst: WorkflowStatus) -> bool:
    return dst in WORKFLOW_TRANSITIONS.get(src, frozenset())


def step_transition_allowed(src: StepStatus, dst: StepStatus) -> bool:
    return src == dst or dst in STEP_TRANSITIONS.get(src, frozenset())
