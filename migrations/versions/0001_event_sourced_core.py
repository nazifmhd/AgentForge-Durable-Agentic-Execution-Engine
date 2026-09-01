"""event-sourced core: definitions, events, snapshots, index

Revision ID: 0001_event_sourced_core
Revises:
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_event_sourced_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_definitions",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("definition", JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tenant_id", "workflow_id", "version"),
        sa.UniqueConstraint("tenant_id", "name", "version", name="uq_defn_name_version"),
    )
    op.create_index(
        "ix_defn_active", "workflow_definitions", ["tenant_id", "name", "is_active"]
    )

    op.create_table(
        "workflow_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instance_id", "sequence", name="uq_event_instance_sequence"
        ),
    )
    op.create_index(
        "ix_events_instance_seq", "workflow_events", ["instance_id", "sequence"]
    )
    op.create_index(
        "ix_events_tenant_recorded", "workflow_events", ["tenant_id", "recorded_at"]
    )
    op.create_index("ix_events_type", "workflow_events", ["event_type"])

    op.create_table(
        "instance_snapshots",
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("instance_id"),
    )
    op.create_index("ix_snapshots_tenant", "instance_snapshots", ["tenant_id"])

    op.create_table(
        "instance_index",
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(64), nullable=False),
        sa.Column("workflow_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cost_accumulated_usd", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("budget_limit_usd", sa.Float(), nullable=True),
        sa.Column("next_wakeup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("instance_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "workflow_version"],
            [
                "workflow_definitions.tenant_id",
                "workflow_definitions.workflow_id",
                "workflow_definitions.version",
            ],
            name="fk_index_definition",
        ),
    )
    op.create_index(
        "ix_index_tenant_status", "instance_index", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_index_status_wakeup", "instance_index", ["status", "next_wakeup_at"]
    )
    op.create_index("ix_index_updated", "instance_index", ["updated_at"])


def downgrade() -> None:
    op.drop_table("instance_index")
    op.drop_table("instance_snapshots")
    op.drop_table("workflow_events")
    op.drop_table("workflow_definitions")
