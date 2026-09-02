"""API keys + row-level-security scaffolding

Revision ID: 0006_api_keys
Revises: 0005_escalations
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006_api_keys"
down_revision: str | None = "0005_escalations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Belt-and-suspenders isolation: when a connection sets
# ``SET LOCAL agentforge.tenant_id`` (the API does this per request) the policy
# clamps every row to that tenant even if a query forgets its WHERE. The worker
# and migrations leave the GUC unset and see everything.
_RLS_TABLES = (
    "workflow_events",
    "instance_snapshots",
    "instance_index",
    "side_effect_outbox",
    "escalations",
    "dead_letters",
)


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("key_id", sa.String(32), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("principal_name", sa.String(128), nullable=False),
        sa.Column("scopes", JSONB(), nullable=False, server_default="[]"),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key_id"),
    )
    op.create_index("ix_apikey_tenant", "api_keys", ["tenant_id"])

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (
                current_setting('agentforge.tenant_id', true) IS NULL
                OR tenant_id = current_setting('agentforge.tenant_id', true)
            )
            """
        )


def downgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("api_keys")
