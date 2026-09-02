"""side-effect outbox (exactly-once external actions)

Revision ID: 0003_side_effect_outbox
Revises: 0002_leases_and_dead_letters
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003_side_effect_outbox"
down_revision: str | None = "0002_leases_and_dead_letters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "side_effect_outbox",
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("step_id", sa.String(128), nullable=False),
        sa.Column("effect_name", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column(
            "guarantee",
            sa.String(32),
            nullable=False,
            server_default="at_least_once_dedup",
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("provider_ref", sa.String(256), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("compensation_status", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.create_index("ix_outbox_instance", "side_effect_outbox", ["instance_id"])
    op.create_index("ix_outbox_status", "side_effect_outbox", ["status"])


def downgrade() -> None:
    op.drop_table("side_effect_outbox")
