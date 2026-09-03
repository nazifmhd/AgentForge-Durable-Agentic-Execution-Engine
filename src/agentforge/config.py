"""Runtime configuration, loaded from environment / .env.

All settings are validated at process start. Nothing in the codebase reads
``os.environ`` directly — everything flows through :data:`settings`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="AGENTFORGE_",
        extra="ignore",
    )

    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # --- Persistence -------------------------------------------------------
    database_url: str = "postgresql+asyncpg://agentforge:agentforge@localhost:5432/agentforge"
    database_pool_size: int = 10
    database_max_overflow: int = 5
    redis_url: str = "redis://localhost:6379/0"

    # --- Worker / durability --------------------------------------------------
    worker_id: str | None = None  # defaults to hostname:pid at runtime
    lease_seconds: int = 30
    lease_heartbeat_seconds: int = 10
    recovery_scan_interval_seconds: int = 15
    max_concurrent_steps_per_worker: int = 8

    # --- Budgets ---------------------------------------------------------------
    default_workflow_budget_usd: float = 1.0
    org_daily_budget_usd: float = 100.0

    # --- LLM providers -------------------------------------------------------
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    model_registry_path: str = "config/models.yaml"

    # --- Integrations ------------------------------------------------------
    n8n_base_url: str | None = None
    n8n_api_key: str | None = None

    # --- Observability ---------------------------------------------------
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "agentforge"
    worker_metrics_port: int = 0  # >0 exposes Prometheus /metrics on the worker

    # --- API ---------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_allow_origins: list[str] = Field(default_factory=list)
    jwt_secret: str = "dev-only-insecure-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    api_key_pepper: str = "dev-only-insecure-pepper-change-me-in-production"
    rate_limit_per_minute: int = 120
    execute_rate_limit_per_minute: int = 30
    webhook_shared_secret: str | None = None

    @field_validator("database_url", mode="before")
    @classmethod
    def _coerce_async_driver(cls, v: str) -> str:
        if isinstance(v, str) and v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def sync_database_url(self) -> str:
        """psycopg-style URL for Alembic migrations."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
