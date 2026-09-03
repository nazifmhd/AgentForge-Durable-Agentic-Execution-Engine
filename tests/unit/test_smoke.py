"""Phase 0 smoke tests — the scaffold imports and config loads."""

from __future__ import annotations

from agentforge import __version__
from agentforge.config import Settings
from agentforge.exceptions import AgentForgeError, LLMTimeoutError


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_settings_defaults() -> None:
    s = Settings()
    assert s.environment == "test"
    assert str(s.database_url).startswith("postgresql+asyncpg://")
    assert s.sync_database_url.startswith("postgresql+psycopg://")


def test_exception_retryable_classification() -> None:
    assert LLMTimeoutError().retryable is True
    assert AgentForgeError().retryable is False


def test_n8n_idempotent_effects_accepts_csv_or_json() -> None:
    assert Settings(n8n_idempotent_effects="a, b ,c").n8n_idempotent_effects == ["a", "b", "c"]
    assert Settings(n8n_idempotent_effects='["x","y"]').n8n_idempotent_effects == ["x", "y"]
    assert Settings().n8n_idempotent_effects == []
