from __future__ import annotations

import pytest

from agentforge.core.domain.dag import topological_layers, validate_dependencies
from agentforge.exceptions import CyclicDependencyError, WorkflowDefinitionError


def test_linear_layers() -> None:
    layers = topological_layers({"a": [], "b": ["a"], "c": ["b"]})
    assert layers == [["a"], ["b"], ["c"]]


def test_diamond_layers_group_parallel_steps() -> None:
    layers = topological_layers({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})
    assert layers == [["a"], ["b", "c"], ["d"]]


def test_layers_are_deterministically_sorted() -> None:
    deps = {"z": [], "y": [], "x": []}
    assert topological_layers(deps) == [["x", "y", "z"]]


def test_self_dependency_rejected() -> None:
    with pytest.raises(WorkflowDefinitionError, match="itself"):
        validate_dependencies({"a": ["a"]})


def test_unknown_dependency_rejected() -> None:
    with pytest.raises(WorkflowDefinitionError, match="unknown step"):
        validate_dependencies({"a": ["ghost"]})


def test_cycle_detected() -> None:
    with pytest.raises(CyclicDependencyError):
        topological_layers({"a": ["b"], "b": ["a"]})


def test_three_node_cycle_detected() -> None:
    with pytest.raises(CyclicDependencyError, match="a', 'b', 'c"):
        validate_dependencies({"a": ["c"], "b": ["a"], "c": ["b"]})
