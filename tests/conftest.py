"""Shared pytest fixtures."""

from __future__ import annotations

import os

os.environ.setdefault("AGENTFORGE_ENVIRONMENT", "test")
os.environ.setdefault("AGENTFORGE_LOG_JSON", "false")
