# Deployment

One image (`Dockerfile`, `production` target) runs both roles — `CMD` picks
`api` or `worker`. Build with `uv sync --frozen` off `uv.lock` for reproducible
installs; runs as a non-root user; health via `/health/live` + `/health/ready`.

## docker-compose (production-shaped)

`docker-compose.prod.yml` brings up `migrate` (one-shot) → `api` + `worker` ×2,
plus Postgres, Redis, the OTel collector, Prometheus, and a provisioned Grafana.

```bash
cp .env.example .env.prod          # fill in real secrets; also set:
#   POSTGRES_PASSWORD=...  GRAFANA_PASSWORD=...  AGENTFORGE_IMAGE=...(optional)
make prod-up                        # docker compose -f docker-compose.prod.yml up -d --build
```

Point `AGENTFORGE_DATABASE_URL` / `AGENTFORGE_REDIS_URL` at managed services and
delete the `postgres` / `redis` services for anything real.

## Kubernetes

`deploy/k8s/` is a Kustomize base — reference manifests, not a chart:

| file | what |
|---|---|
| `configmap.yaml` | non-secret settings |
| `secret.yaml` | **example** — replace with External Secrets / Vault / SOPS |
| `migrate-job.yaml` | `alembic upgrade head`; ArgoCD PreSync hook |
| `api-deployment.yaml` + `-service.yaml` + `-hpa.yaml` | API, 2–10 replicas on CPU |
| `worker-deployment.yaml` | 3 replicas, 60s graceful drain, `/metrics` on :9100 |
| `servicemonitor.yaml` | `PodMonitor` for the Prometheus Operator |

```bash
# edit kustomization.yaml (image), secret.yaml, configmap.yaml first
kubectl apply -k deploy/k8s
```

Scaling: the API scales on CPU via the HPA. Workers scale by replica count —
they're lease-coordinated, so there's no cap beyond Postgres connections. A
rolling update is safe mid-workflow (expired leases get reclaimed from the event
log).

## Observability

Set `AGENTFORGE_OTEL_EXPORTER_OTLP_ENDPOINT` to ship traces; set
`AGENTFORGE_WORKER_METRICS_PORT` so workers expose `/metrics`. Prometheus scrape
config is in `deploy/prometheus.yml`; the Grafana overview dashboard is in
`deploy/grafana/dashboards/`. See [operations.md](operations.md).
