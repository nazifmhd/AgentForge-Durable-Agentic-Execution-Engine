"""Recovery visibility.

The *mechanism* of recovery is spread across three places by design:

* an expired lease makes an instance claimable again
  (:meth:`agentforge.core.leasing.PgLeaseStore.acquire_runnable`);
* the driver resets any step a dead worker left ``RUNNING`` back to ``READY``
  (:meth:`agentforge.core.driver.WorkflowDriver._recover_in_flight`);
* snapshots + tail-fold bound how much is replayed on resume (Phase 1).

This service adds *observation* on top: how many instances are currently orphaned
(runnable, unleased, and stale) so a worker log line or a health check can show
recovery is keeping up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    scanned_at: datetime
    orphaned_instances: list[str]
    expired_leases: int

    @property
    def healthy(self) -> bool:
        return len(self.orphaned_instances) == 0


class RecoveryService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        stale_after_seconds: int = 120,
    ) -> None:
        self._sm = sessionmaker
        self._stale = timedelta(seconds=stale_after_seconds)

    async def scan(self, now: datetime) -> RecoveryReport:
        cutoff = now - self._stale
        async with self._sm() as session:
            orphaned = (
                await session.execute(
                    text(
                        """
                        SELECT i.instance_id
                        FROM instance_index i
                        LEFT JOIN instance_leases l ON l.instance_id = i.instance_id
                        WHERE i.status IN ('pending', 'running', 'retrying')
                          AND (i.next_wakeup_at IS NULL OR i.next_wakeup_at <= :now)
                          AND (l.instance_id IS NULL OR l.expires_at < :now)
                          AND i.updated_at < :cutoff
                        """
                    ),
                    {"now": now, "cutoff": cutoff},
                )
            ).all()
            expired = await session.scalar(
                text("SELECT count(*) FROM instance_leases WHERE expires_at < :now"),
                {"now": now},
            )
        return RecoveryReport(
            scanned_at=now,
            orphaned_instances=[r.instance_id for r in orphaned],
            expired_leases=int(expired or 0),
        )
