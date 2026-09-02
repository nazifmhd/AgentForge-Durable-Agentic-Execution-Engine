"""escalations read model (human-in-the-loop)

Revision ID: 0005_escalations
Revises: 0004_tenant_cost_ledger
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005_escalations"
down_revision: str | None = "0004_tenant_cost_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "escalations",
        sa.Column("escalation_id", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("step_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("recommendation", sa.String(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("options", JSONB(), nullable=False, server_default="[]"),
        sa.Column("auto_action", sa.String(16), nullable=False, server_default="abort"),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("resolution", sa.String(16), nullable=True),
        sa.Column("resolved_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("escalation_id"),
    )
    op.create_index("ix_esc_tenant_status", "escalations", ["tenant_id", "status"])
    op.create_index("ix_esc_deadline", "escalations", ["status", "deadline"])
    op.create_index("ix_esc_instance", "escalations", ["instance_id"])


def downgrade() -> None:
    op.drop_table("escalations")
