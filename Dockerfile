# syntax=docker/dockerfile:1.9
# Multi-stage build. `uv` for fast, reproducible installs from uv.lock.

FROM python:3.12-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /bin/
WORKDIR /app

# ---- dependency layer (cached unless lockfile changes) --------------------
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra agents

# ---- build the project ---------------------------------------------------
FROM deps AS build
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra agents

# ---- runtime -----------------------------------------------------------
FROM base AS production
RUN groupadd --system app && useradd --system --gid app --home /app app
COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH"
USER app
EXPOSE 8000
# Liveness/readiness handled by the orchestrator hitting /health/live & /health/ready
ENTRYPOINT ["agentforge"]
CMD ["api"]
