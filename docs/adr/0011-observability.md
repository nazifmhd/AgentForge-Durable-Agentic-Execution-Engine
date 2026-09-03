# ADR-0011: Observability — structured logs, Prometheus metrics, OTLP traces

- **Status:** accepted
- **Date:** 2026-09-02

## Context

"Reliability" and "Cost Efficiency" are only real if they are measurable in
production. We need per-step latency, LLM spend, side-effect and escalation
rates, and request/step traces — without coupling `core` to a vendor SDK or a
running collector.

## Decision

Three signals, one `configure_observability()` call made by every entrypoint
(`agentforge api`, `agentforge worker`, tests that need it):

- **Logs** — structlog, JSON in every non-local env. A processor binds the
  active OTel `trace_id` / `span_id` onto every line, so logs join traces.
- **Metrics** — `prometheus_client` on the default registry. `core` calls thin
  `agentforge.observability.metrics.record_*` helpers; they are cheap and always
  safe (a worker with no HTTP surface still accumulates them). The API exposes
  `/metrics`; a worker exposes its own `/metrics` when
  `AGENTFORGE_WORKER_METRICS_PORT` is set.
- **Traces** — OpenTelemetry. `configure_tracing()` installs an SDK
  `TracerProvider` once (OTel forbids replacing it) and only ever adds span
  processors. Spans are exported over OTLP when
  `AGENTFORGE_OTEL_EXPORTER_OTLP_ENDPOINT` is set; otherwise spans are still
  created (for context propagation and the log binding) but not exported. The
  driver opens `workflow.drive` and `workflow.step` spans; the LLM client opens
  `llm.complete`. FastAPI is auto-instrumented.

## Consequences

- `core` imports `agentforge.observability` (first-party, always installed), not
  `prometheus_client` / `opentelemetry` directly beyond the metrics + tracing
  modules. No collector required to run.
- Metric objects are process-global singletons — tests assert on deltas, not
  absolute values.
- The OTLP exporter and FastAPI instrumentation are imported lazily so a missing
  optional package degrades to "no traces", never a crash.

## Alternatives considered

- **OpenTelemetry metrics instead of Prometheus** — heavier, and Prometheus
  scraping is the common denominator for the target deploy. Revisit if the OTel
  metrics API stabilises further.
- **A metrics facade in `core` with a no-op default** — over-engineered; the
  `record_*` helpers already are the seam.
