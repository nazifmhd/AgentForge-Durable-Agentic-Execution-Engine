"""SQLAlchemy table definitions for the event-sourced core.

- ``workflow_definitions`` — registered templates, versioned per tenant.
- ``workflow_events``      — the append-only log. ``UNIQUE(instance_id, sequence)``
  is the optimistic-concurrency guard (ADR-0002/0009).
- ``instance_snapshots``   — one row per instance, the latest fold.
- ``instance_index``       — mutable read model for cheap status queries and the
  Phase 2 worker claim; rebuilt from events, never authoritative.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentforge.db import Base


class WorkflowDefinitionRow(Base):
    __tablename__ = "workflow_definitions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "workflow_id", "version"),
        UniqueConstraint("tenant_id", "name", "version", name="uq_defn_name_version"),
        Index("ix_defn_active", "tenant_id", "name", "is_active"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(String, default="")
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkflowEventRow(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        UniqueConstraint("instance_id", "sequence", name="uq_event_instance_sequence"),
        Index("ix_events_instance_seq", "instance_id", "sequence"),
        Index("ix_events_tenant_recorded", "tenant_id", "recorded_at"),
        Index("ix_events_type", "event_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InstanceSnapshotRow(Base):
    __tablename__ = "instance_snapshots"
    __table_args__ = (
        PrimaryKeyConstraint("instance_id"),
        Index("ix_snapshots_tenant", "tenant_id"),
    )

    instance_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstanceIndexRow(Base):
    __tablename__ = "instance_index"
    __table_args__ = (
        PrimaryKeyConstraint("instance_id"),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "workflow_version"],
            [
                "workflow_definitions.tenant_id",
                "workflow_definitions.workflow_id",
                "workflow_definitions.version",
            ],
            name="fk_index_definition",
        ),
        Index("ix_index_tenant_status", "tenant_id", "status"),
        Index("ix_index_status_wakeup", "status", "next_wakeup_at"),
        Index("ix_index_updated", "updated_at"),
    )

    instance_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(64))
    workflow_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    cost_accumulated_usd: Mapped[float] = mapped_column(Float, default=0.0)
    budget_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_wakeup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InstanceLeaseRow(Base):
    __tablename__ = "instance_leases"
    __table_args__ = (
        PrimaryKeyConstraint("instance_id"),
        Index("ix_leases_expires", "expires_at"),
        Index("ix_leases_worker", "worker_id"),
    )

    instance_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(64))
    worker_id: Mapped[str] = mapped_column(String(128))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fence_token: Mapped[int] = mapped_column(BigInteger, default=1)


class DeadLetterRow(Base):
    __tablename__ = "dead_letters"
    __table_args__ = (
        PrimaryKeyConstraint("id"),
        Index("ix_dlq_tenant_created", "tenant_id", "created_at"),
        Index("ix_dlq_resolved", "resolved"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(64))
    step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str] = mapped_column(String(256))
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    at_version: Mapped[int] = mapped_column(Integer)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SideEffectOutboxRow(Base):
    __tablename__ = "side_effect_outbox"
    __table_args__ = (
        PrimaryKeyConstraint("idempotency_key"),
        Index("ix_outbox_instance", "instance_id"),
        Index("ix_outbox_status", "status"),
    )

    idempotency_key: Mapped[str] = mapped_column(String(64))
    instance_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(64))
    step_id: Mapped[str] = mapped_column(String(128))
    effect_name: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    guarantee: Mapped[str] = mapped_column(String(32), default="at_least_once_dedup")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    compensation_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
