"""The LangGraph agent runtime (ADR-0006).

Four general-purpose agents, each a ``StepRunner``: planner, executor, validator,
reflector. Use-case-specific agents (Phase 8) subclass ``BaseAgent`` the same way.
"""

from agentforge.agents.base_agent import BaseAgent, ctx_of
from agentforge.agents.executor_agent import ExecutorAgent
from agentforge.agents.planner_agent import PlannerAgent
from agentforge.agents.reflector_agent import ReflectorAgent
from agentforge.agents.tools import (
    AgentTool,
    EffectTool,
    FunctionTool,
    ToolRegistry,
)
from agentforge.agents.validator_agent import ValidatorAgent

__all__ = [
    "AgentTool",
    "BaseAgent",
    "EffectTool",
    "ExecutorAgent",
    "FunctionTool",
    "PlannerAgent",
    "ReflectorAgent",
    "ToolRegistry",
    "ValidatorAgent",
    "ctx_of",
    "register_base_agents",
]


def register_base_agents(registry: "object", *, tools: "ToolRegistry | None" = None) -> None:
    """Register the four base agents on a ``StepRegistry``."""
    from agentforge.core.runners import StepRegistry

    assert isinstance(registry, StepRegistry)
    registry.register("planner_agent", PlannerAgent())
    registry.register("executor_agent", ExecutorAgent(tools))
    registry.register("validator_agent", ValidatorAgent())
    registry.register("reflector_agent", ReflectorAgent())
