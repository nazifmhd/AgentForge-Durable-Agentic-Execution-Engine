"""instance leases + dead-letter queue

Revision ID: 0002_leases_and_dead_letters
Revises: 0001_event_sourced_core
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_leases_and_dead_letters"
down_revision: str | None = "0001_event_sourced_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instance_leases",
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("instance_id"),
    )
    op.create_index("ix_leases_expires", "instance_leases", ["expires_at"])
    op.create_index("ix_leases_worker", "instance_leases", ["worker_id"])

    op.create_table(
        "dead_letters",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("step_id", sa.String(128), nullable=True),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("at_version", sa.Integer(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dlq_tenant_created", "dead_letters", ["tenant_id", "created_at"]
    )
    op.create_index("ix_dlq_resolved", "dead_letters", ["resolved"])


def downgrade() -> None:
    op.drop_table("dead_letters")
    op.drop_table("instance_leases")
