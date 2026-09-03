"""The Alembic chain must run end-to-end (offline) with no missing driver.

This is the fast guard for ``migrations/env.py`` and the revision files — the
integration suite uses ``Base.metadata.create_all`` and never exercises Alembic,
so a broken migration or a missing sync driver would otherwise only surface at
deploy time.
"""

from __future__ import annotations

from pathlib import Path

from alembic.command import upgrade
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[2]


def _config() -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    return cfg


def test_single_head() -> None:
    heads = ScriptDirectory.from_config(_config()).get_heads()
    assert len(heads) == 1, f"expected one migration head, found {heads}"


def test_full_chain_generates_sql_offline(capsys) -> None:
    # --sql mode: no database needed, but env.py + every revision still execute.
    upgrade(_config(), "head", sql=True)
    emitted = capsys.readouterr().out
    for table in (
        "workflow_definitions",
        "workflow_events",
        "instance_snapshots",
        "instance_index",
        "instance_leases",
        "dead_letters",
        "side_effect_outbox",
        "tenant_cost_ledger",
        "escalations",
        "api_keys",
    ):
        assert f"CREATE TABLE {table}" in emitted
    assert "ENABLE ROW LEVEL SECURITY" in emitted  # 0006
