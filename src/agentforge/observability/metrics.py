"""Prometheus metrics for the engine.

Metric objects register on the default ``prometheus_client`` registry, so the
API's ``/metrics`` route exposes them with no extra wiring. ``core`` calls the
``record_*`` helpers — they are cheap and always safe to call (a worker with no
HTTP surface still accumulates them; they are simply never scraped).

Naming follows the Prometheus conventions: ``agentforge_<subsystem>_<unit>``,
counters end ``_total``, durations are ``_seconds`` histograms.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_STEP_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)
_COST_BUCKETS = (0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5)

STEPS_TOTAL = Counter(
    "agentforge_steps_total",
    "Step attempts by agent type and outcome.",
    ("agent_type", "outcome"),
)
STEP_DURATION = Histogram(
    "agentforge_step_duration_seconds",
    "Wall time of a step attempt.",
    ("agent_type",),
    buckets=_STEP_BUCKETS,
)
DRIVES_TOTAL = Counter(
    "agentforge_workflow_drives_total",
    "Completed driver passes by result.",
    ("result",),
)
LLM_REQUESTS_TOTAL = Counter(
    "agentforge_llm_requests_total",
    "LLM provider calls by model and outcome.",
    ("model", "outcome"),
)
LLM_TOKENS_TOTAL = Counter(
    "agentforge_llm_tokens_total",
    "Tokens billed by model and direction.",
    ("model", "direction"),
)
LLM_COST_USD_TOTAL = Counter(
    "agentforge_llm_cost_usd_total",
    "Estimated LLM spend in USD by model.",
    ("model",),
)
LLM_COST_USD = Histogram(
    "agentforge_llm_call_cost_usd",
    "Per-call LLM cost in USD.",
    buckets=_COST_BUCKETS,
)
SIDE_EFFECTS_TOTAL = Counter(
    "agentforge_side_effects_total",
    "Side effects by name, guarantee, and outcome.",
    ("effect", "guarantee", "outcome"),
)
ESCALATIONS_TOTAL = Counter(
    "agentforge_escalations_total",
    "Escalations raised by reason.",
    ("reason",),
)
DEAD_LETTERS_TOTAL = Counter(
    "agentforge_dead_letters_total",
    "Instances sent to the dead-letter queue.",
)
BUDGET_REFUSALS_TOTAL = Counter(
    "agentforge_budget_refusals_total",
    "Pre-flight budget refusals by scope.",
    ("scope",),
)
ACTIVE_LEASES = Gauge(
    "agentforge_active_leases",
    "Instances currently leased by this worker.",
    ("worker_id",),
)


def record_step(agent_type: str, outcome: str, duration_seconds: float) -> None:
    STEPS_TOTAL.labels(agent_type=agent_type, outcome=outcome).inc()
    STEP_DURATION.labels(agent_type=agent_type).observe(max(duration_seconds, 0.0))


def record_drive(result: str) -> None:
    DRIVES_TOTAL.labels(result=result).inc()


def record_llm(
    model: str, *, outcome: str, tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0
) -> None:
    LLM_REQUESTS_TOTAL.labels(model=model, outcome=outcome).inc()
    if tokens_in:
        LLM_TOKENS_TOTAL.labels(model=model, direction="input").inc(tokens_in)
    if tokens_out:
        LLM_TOKENS_TOTAL.labels(model=model, direction="output").inc(tokens_out)
    if cost_usd:
        LLM_COST_USD_TOTAL.labels(model=model).inc(cost_usd)
        LLM_COST_USD.observe(cost_usd)


def record_side_effect(effect: str, *, guarantee: str, outcome: str) -> None:
    SIDE_EFFECTS_TOTAL.labels(effect=effect, guarantee=guarantee, outcome=outcome).inc()


def record_escalation(reason: str) -> None:
    ESCALATIONS_TOTAL.labels(reason=reason).inc()


def record_dead_letter() -> None:
    DEAD_LETTERS_TOTAL.inc()


def record_budget_refusal(scope: str) -> None:
    BUDGET_REFUSALS_TOTAL.labels(scope=scope).inc()


def set_active_leases(worker_id: str, count: int) -> None:
    ACTIVE_LEASES.labels(worker_id=worker_id).set(count)
