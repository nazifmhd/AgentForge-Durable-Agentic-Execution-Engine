"""Phase 9 — metrics + tracing wiring."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from tests.doubles import (
    FakeDeadLetters,
    InMemoryDefinitions,
    InMemoryJournal,
    InMemoryLeaseStore,
    seed_instance,
)
from tests.factories import make_step

from agentforge.core.domain.definition import WorkflowDefinition
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.executor import StepExecutor
from agentforge.core.ports import FixedClock, SequentialIdGenerator
from agentforge.core.runners import FunctionRunner, StepContext, StepRegistry, StepResult
from agentforge.logging import _add_trace_context
from agentforge.observability import metrics
from agentforge.observability.tracing import _reset_for_tests, configure_tracing, span

T0 = datetime(2026, 9, 1, tzinfo=UTC)
TENANT = "tenant-1"


def _sample(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# --- tracing --------------------------------------------------------


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    _reset_for_tests()
    exporter = InMemorySpanExporter()
    configure_tracing(extra_processor=SimpleSpanProcessor(exporter))
    yield exporter
    _reset_for_tests()


def test_span_records_attributes(spans: InMemorySpanExporter) -> None:
    with span("unit.work", instance_id="inst-1", count=3, skip=None):
        pass

    (finished,) = spans.get_finished_spans()
    assert finished.name == "unit.work"
    assert finished.attributes["instance_id"] == "inst-1"
    assert finished.attributes["count"] == 3
    assert "skip" not in finished.attributes  # None is dropped


def test_span_marks_status_error_on_exception(spans: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError, match="boom"), span("unit.fail"):
        raise ValueError("boom")

    (finished,) = spans.get_finished_spans()
    assert finished.status.status_code.name == "ERROR"
    assert finished.events  # the recorded exception


def test_add_trace_context_binds_ids_inside_a_span(spans: InMemorySpanExporter) -> None:
    with span("unit.log"):
        event = _add_trace_context(None, "info", {"event": "hi"})
    assert len(event["trace_id"]) == 32
    assert len(event["span_id"]) == 16


def test_add_trace_context_is_a_noop_outside_a_span() -> None:
    _reset_for_tests()
    event = _add_trace_context(None, "info", {"event": "hi"})
    assert "trace_id" not in event


# --- metrics -------------------------------------------------------


def test_record_helpers_move_counters() -> None:
    before = _sample("agentforge_llm_requests_total", {"model": "m-x", "outcome": "ok"})
    metrics.record_llm("m-x", outcome="ok", tokens_in=100, tokens_out=20, cost_usd=0.002)
    after = _sample("agentforge_llm_requests_total", {"model": "m-x", "outcome": "ok"})
    assert after == before + 1
    assert _sample("agentforge_llm_tokens_total", {"model": "m-x", "direction": "input"}) >= 100
    assert _sample("agentforge_llm_cost_usd_total", {"model": "m-x"}) >= 0.002


async def test_driver_emits_step_and_drive_metrics() -> None:
    reg = StepRegistry()

    async def work(ctx: StepContext) -> StepResult:
        return StepResult(output={"did": ctx.step_id})

    reg.register("executor_agent", FunctionRunner(work))

    journal = InMemoryJournal()
    leases = InMemoryLeaseStore(journal)
    defs = InMemoryDefinitions()
    driver = WorkflowDriver(
        journal,
        defs,
        StepExecutor(reg),
        FakeDeadLetters(),  # type: ignore[arg-type]
        clock=FixedClock(T0),
        ids=SequentialIdGenerator("ev"),
    )
    wf = WorkflowDefinition(
        workflow_id="w",
        name="w",
        version="1.0.0",
        steps=(make_step("a"), make_step("b", ("a",))),
    )
    defs.add(wf)
    await seed_instance(journal, wf)

    completed_before = _sample("agentforge_workflow_drives_total", {"result": "completed"})
    steps_before = _sample(
        "agentforge_steps_total", {"agent_type": "executor_agent", "outcome": "success"}
    )

    lease = (await leases.acquire_runnable("w1", 5, T0))[0]
    report = await driver.drive(lease, leases.make_guard(lease))
    assert report.result is DriveResult.COMPLETED

    assert _sample("agentforge_workflow_drives_total", {"result": "completed"}) == (
        completed_before + 1
    )
    assert _sample(
        "agentforge_steps_total", {"agent_type": "executor_agent", "outcome": "success"}
    ) == (steps_before + 2)
