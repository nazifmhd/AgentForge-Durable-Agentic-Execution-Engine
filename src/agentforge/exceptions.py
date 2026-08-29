"""Custom exception hierarchy.

The ``retryable`` flag drives the executor's retry decision — a policy can also
override per-exception-name, but this is the default classification.
"""

from __future__ import annotations


class AgentForgeError(Exception):
    """Base class for every error raised by the engine."""

    retryable: bool = False


# --- Configuration / definition errors (never retryable) -------------------
class ConfigurationError(AgentForgeError):
    pass


class WorkflowDefinitionError(ConfigurationError):
    pass


class CyclicDependencyError(WorkflowDefinitionError):
    pass


# --- Concurrency / persistence -----------------------------------------
class ConflictError(AgentForgeError):
    """Optimistic-concurrency or lease-ownership conflict."""

    retryable = True


class LeaseLostError(ConflictError):
    """This worker no longer owns the instance lease."""


# --- Execution ----------------------------------------------------------
class StepExecutionError(AgentForgeError):
    pass


class StepTimeoutError(StepExecutionError):
    retryable = True


class MaxRetriesExceededError(StepExecutionError):
    retryable = False


class BudgetExceededError(AgentForgeError):
    retryable = False


# --- LLM / agent runtime ------------------------------------------------
class LLMError(AgentForgeError):
    pass


class LLMTimeoutError(LLMError):
    retryable = True


class RateLimitError(LLMError):
    retryable = True


class MalformedOutputError(LLMError):
    retryable = True


class NoEligibleModelError(LLMError):
    retryable = False


# --- Side effects ------------------------------------------------------
class SideEffectError(AgentForgeError):
    pass


class CompensationError(SideEffectError):
    """A rollback/compensation action itself failed — needs human attention."""

    retryable = False


# --- Human-in-the-loop -----------------------------------------------
class EscalationTimeout(AgentForgeError):
    retryable = False


RETRYABLE_BY_NAME: frozenset[str] = frozenset(
    {
        "LLMTimeoutError",
        "RateLimitError",
        "MalformedOutputError",
        "StepTimeoutError",
        "ConflictError",
        "ConnectionError",
        "TimeoutError",
    }
)
