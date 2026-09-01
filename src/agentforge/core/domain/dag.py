"""Dependency-graph validation and layering for workflow steps.

Kahn's algorithm gives us both cycle detection and the parallel-execution layers
the scheduler uses to honour ``max_concurrent_steps``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentforge.exceptions import CyclicDependencyError, WorkflowDefinitionError


def validate_dependencies(dependencies: Mapping[str, Sequence[str]]) -> None:
    """Raise if a dependency references an unknown step or the graph has a cycle."""
    known = set(dependencies)
    for step_id, deps in dependencies.items():
        for dep in deps:
            if dep == step_id:
                raise WorkflowDefinitionError(f"step {step_id!r} depends on itself")
            if dep not in known:
                raise WorkflowDefinitionError(f"step {step_id!r} depends on unknown step {dep!r}")
    # cycle detection falls out of a failed topological sort
    topological_layers(dependencies)


def topological_layers(
    dependencies: Mapping[str, Sequence[str]],
) -> list[list[str]]:
    """Group steps into ordered layers; every step in layer *i* depends only on
    steps in layers ``< i``. Steps within a layer can run concurrently.

    Layer order and within-layer order are deterministic (sorted) so plans and
    tests are stable.
    """
    remaining: dict[str, set[str]] = {step_id: set(deps) for step_id, deps in dependencies.items()}
    layers: list[list[str]] = []
    resolved: set[str] = set()

    while remaining:
        ready = sorted(step_id for step_id, deps in remaining.items() if deps <= resolved)
        if not ready:
            cycle = sorted(remaining)
            raise CyclicDependencyError(f"cyclic or unresolvable dependencies among steps: {cycle}")
        layers.append(ready)
        resolved.update(ready)
        for step_id in ready:
            del remaining[step_id]

    return layers
