"""Load the Ideal Customer Profile from YAML (deployment config, like the model
registry). Falls back to a permissive default so the workflow is runnable with
no config file present."""

from __future__ import annotations

from pathlib import Path

import yaml

from agentforge.exceptions import ConfigurationError
from agentforge.use_cases.sales_intelligence.models import ICPProfile

DEFAULT_ICP_PATH = "config/sales_intelligence.yaml"


def load_icp(path: str | Path = DEFAULT_ICP_PATH) -> ICPProfile:
    p = Path(path)
    if not p.is_file():
        raise ConfigurationError(f"ICP profile not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return ICPProfile.model_validate(raw)


def default_icp() -> ICPProfile:
    """A profile that qualifies most leads — used when no YAML is configured."""
    return ICPProfile(name="permissive-default")
