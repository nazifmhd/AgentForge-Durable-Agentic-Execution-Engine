"""Instance snapshots — bound replay cost.

A snapshot stores a folded ``WorkflowInstance`` at a known ``version``. To read
current state: load the latest snapshot, then ``fold`` only the events after it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from agentforge.core.domain.instance import WorkflowInstance

# Fold this many tail events → time to write a fresh snapshot.
SNAPSHOT_EVERY = 50


class Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    tenant_id: str
    version: int
    state: WorkflowInstance
    created_at: datetime

    @classmethod
    def of(cls, instance: WorkflowInstance, *, created_at: datetime) -> Snapshot:
        return cls(
            instance_id=instance.instance_id,
            tenant_id=instance.tenant_id,
            version=instance.version,
            state=instance,
            created_at=created_at,
        )


def should_snapshot(last_snapshot_version: int, current_version: int) -> bool:
    return current_version - last_snapshot_version >= SNAPSHOT_EVERY
