"""per-tenant daily cost ledger

Revision ID: 0004_tenant_cost_ledger
Revises: 0003_side_effect_outbox
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_tenant_cost_ledger"
down_revision: str | None = "0003_side_effect_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_cost_ledger",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tenant_id", "day"),
    )


def downgrade() -> None:
    op.drop_table("tenant_cost_ledger")
