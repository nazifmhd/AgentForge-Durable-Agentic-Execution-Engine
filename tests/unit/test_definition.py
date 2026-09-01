from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.factories import diamond_workflow, linear_workflow, make_step

from agentforge.core.domain.definition import RetryPolicy, WorkflowDefinition
from agentforge.exceptions import CyclicDependencyError, WorkflowDefinitionError


def test_retry_backoff_is_capped_and_deterministic() -> None:
    p = RetryPolicy(backoff_base_seconds=1, backoff_multiplier=2, backoff_max_seconds=10)
    assert [p.backoff_delay(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 4, 8, 10]


def test_checksum_is_content_addressed() -> None:
    assert linear_workflow(3).checksum == linear_workflow(3).checksum
    assert linear_workflow(3).checksum != linear_workflow(4).checksum


def test_execution_layers() -> None:
    assert diamond_workflow().execution_layers() == [["a"], ["b", "c"], ["d"]]


def test_dependents_lookup() -> None:
    assert diamond_workflow().dependents("a") == ("b", "c")


def test_duplicate_step_ids_rejected() -> None:
    with pytest.raises(WorkflowDefinitionError, match="duplicate"):
        WorkflowDefinition(
            workflow_id="w",
            name="w",
            version="1.0.0",
            steps=(make_step("x"), make_step("x")),
        )


def test_cycle_in_definition_rejected() -> None:
    with pytest.raises(CyclicDependencyError):
        WorkflowDefinition(
            workflow_id="w",
            name="w",
            version="1.0.0",
            steps=(make_step("a", ("b",)), make_step("b", ("a",))),
        )


def test_compensation_without_side_effects_rejected() -> None:
    with pytest.raises(WorkflowDefinitionError, match="compensation_action"):
        WorkflowDefinition(
            workflow_id="w",
            name="w",
            version="1.0.0",
            steps=(make_step("a", compensation_action="undo"),),
        )


def test_bad_version_string_rejected() -> None:
    with pytest.raises(ValidationError):
        linear_workflow(2, version="v1")


def test_empty_workflow_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition(workflow_id="w", name="w", version="1.0.0", steps=())


def test_roundtrips_through_json() -> None:
    defn = diamond_workflow()
    assert WorkflowDefinition.model_validate(defn.model_dump(mode="json")) == defn
