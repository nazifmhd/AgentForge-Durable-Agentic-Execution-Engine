"""The cost-aware model registry — loaded from ``config/models.yaml`` (ADR-0008).

No model id or price is hardcoded in Python. The registry is reloadable so a
pricing change is a config edit + a signal, not a deploy of new code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agentforge.core.domain.enums import CostTier
from agentforge.exceptions import ConfigurationError


class ModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    model_id: str
    provider: str
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    avg_latency_ms: int = Field(ge=0)
    reliability_score: float = Field(ge=0, le=1)
    supports_tools: bool = True
    supports_vision: bool = False
    tiers: tuple[CostTier, ...] = ()

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_per_mtok
            + output_tokens / 1_000_000 * self.output_per_mtok
        )


class ModelRegistry:
    def __init__(
        self,
        models: dict[str, ModelConfig],
        fallback_chains: dict[CostTier, tuple[str, ...]],
    ) -> None:
        self._models = models
        self._chains = fallback_chains

    # --- lookups -----------------------------------------------------
    def get(self, key: str) -> ModelConfig:
        try:
            return self._models[key]
        except KeyError:
            raise ConfigurationError(f"unknown model {key!r}") from None

    def all(self) -> list[ModelConfig]:
        return list(self._models.values())

    def for_tier(self, tier: CostTier) -> list[ModelConfig]:
        eligible = [m for m in self._models.values() if tier in m.tiers]
        return sorted(eligible, key=lambda m: m.cost_usd(1_000_000, 1_000_000))

    def fallback_chain(self, tier: CostTier) -> list[str]:
        return list(self._chains.get(tier, ()))

    # --- loading ---------------------------------------------------
    @classmethod
    def from_path(cls, path: str | Path) -> ModelRegistry:
        p = Path(path)
        if not p.is_file():
            raise ConfigurationError(f"model registry not found: {p}")
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ModelRegistry:
        models_raw = raw.get("models") or {}
        if not models_raw:
            raise ConfigurationError("model registry has no 'models'")
        models = {key: ModelConfig(key=key, **spec) for key, spec in models_raw.items()}
        chains: dict[CostTier, tuple[str, ...]] = {}
        for tier_name, keys in (raw.get("fallback_chains") or {}).items():
            tier = CostTier(tier_name)
            for k in keys:
                if k not in models:
                    raise ConfigurationError(
                        f"fallback_chains[{tier_name}] references unknown model {k!r}"
                    )
            chains[tier] = tuple(keys)
        return cls(models, chains)

    def reload(self, path: str | Path) -> None:
        fresh = ModelRegistry.from_path(path)
        self._models = fresh._models
        self._chains = fresh._chains
