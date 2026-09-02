"""Budget tracking — per-workflow and per-tenant-per-day.

The per-workflow limit lives on the instance (``budget_limit_usd``) and its
remaining amount falls out of the fold. The per-tenant daily spend is a rollup
in ``tenant_cost_ledger``, bumped every time the driver appends a ``CostCharged``
event. :class:`BudgetService.view` combines both into a :class:`BudgetView` the
cost router checks *before* a model call (ADR-0008).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge.config import settings
from agentforge.core.domain.instance import WorkflowInstance
from agentforge.core.persistence.tables import TenantCostLedgerRow


@dataclass(frozen=True, slots=True)
class BudgetView:
    remaining_workflow_usd: float | None
    remaining_tenant_daily_usd: float | None

    def allows(self, projected_usd: float) -> bool:
        limits = (self.remaining_workflow_usd, self.remaining_tenant_daily_usd)
        return all(lim is None or projected_usd <= lim for lim in limits)

    def tightest_remaining(self) -> float | None:
        vals = [
            v
            for v in (self.remaining_workflow_usd, self.remaining_tenant_daily_usd)
            if v is not None
        ]
        return min(vals) if vals else None


class BudgetLedger(Protocol):
    async def spent_today(self, tenant_id: str, day: date) -> float: ...
    async def record(self, tenant_id: str, day: date, amount_usd: float) -> None: ...


class PgBudgetLedger:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def spent_today(self, tenant_id: str, day: date) -> float:
        async with self._sm() as session:
            val = await session.scalar(
                select(TenantCostLedgerRow.cost_usd).where(
                    TenantCostLedgerRow.tenant_id == tenant_id,
                    TenantCostLedgerRow.day == day,
                )
            )
            return float(val or 0.0)

    async def record(self, tenant_id: str, day: date, amount_usd: float) -> None:
        if amount_usd <= 0:
            return
        stmt = pg_insert(TenantCostLedgerRow).values(
            tenant_id=tenant_id, day=day, cost_usd=amount_usd
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[TenantCostLedgerRow.tenant_id, TenantCostLedgerRow.day],
            set_={"cost_usd": TenantCostLedgerRow.cost_usd + amount_usd},
        )
        async with self._sm() as session, session.begin():
            await session.execute(stmt)


class InMemoryBudgetLedger:
    def __init__(self) -> None:
        self._spend: dict[tuple[str, date], float] = {}

    async def spent_today(self, tenant_id: str, day: date) -> float:
        return self._spend.get((tenant_id, day), 0.0)

    async def record(self, tenant_id: str, day: date, amount_usd: float) -> None:
        if amount_usd <= 0:
            return
        self._spend[(tenant_id, day)] = self._spend.get((tenant_id, day), 0.0) + amount_usd


class BudgetService:
    def __init__(
        self,
        ledger: BudgetLedger,
        *,
        tenant_daily_limit_usd: float | None = None,
    ) -> None:
        self._ledger = ledger
        self._daily_limit = (
            tenant_daily_limit_usd
            if tenant_daily_limit_usd is not None
            else settings.org_daily_budget_usd
        )

    async def view(self, instance: WorkflowInstance, *, now: datetime) -> BudgetView:
        remaining_wf = instance.remaining_budget_usd
        remaining_daily: float | None = None
        if self._daily_limit is not None:
            spent = await self._ledger.spent_today(instance.tenant_id, now.date())
            remaining_daily = self._daily_limit - spent
        return BudgetView(
            remaining_workflow_usd=remaining_wf,
            remaining_tenant_daily_usd=remaining_daily,
        )

    async def record_spend(self, tenant_id: str, amount_usd: float, *, now: datetime) -> None:
        await self._ledger.record(tenant_id, now.date(), amount_usd)
