from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest

from agentforge.core.cost.budget import BudgetService, PgBudgetLedger
from agentforge.core.domain.instance import WorkflowInstance

pytestmark = pytest.mark.integration


async def test_ledger_accumulates_concurrently(sessionmaker) -> None:
    ledger = PgBudgetLedger(sessionmaker)
    day = date(2026, 9, 1)

    await asyncio.gather(*(ledger.record("t1", day, 0.25) for _ in range(8)))
    assert await ledger.spent_today("t1", day) == pytest.approx(2.0)
    assert await ledger.spent_today("t2", day) == 0.0


async def test_budget_service_view_against_real_ledger(sessionmaker) -> None:
    svc = BudgetService(PgBudgetLedger(sessionmaker), tenant_daily_limit_usd=5.0)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    await svc.record_spend("t1", 3.5, now=now)

    inst = WorkflowInstance(
        instance_id="i",
        tenant_id="t1",
        workflow_id="w",
        workflow_version="1.0.0",
        budget_limit_usd=1.0,
        cost_accumulated_usd=0.25,
    )
    view = await svc.view(inst, now=now)
    assert view.remaining_workflow_usd == pytest.approx(0.75)
    assert view.remaining_tenant_daily_usd == pytest.approx(1.5)
    assert view.allows(0.5) is True
    assert view.allows(0.9) is False  # workflow budget is the tighter one
