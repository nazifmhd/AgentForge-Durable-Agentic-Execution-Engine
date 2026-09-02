"""The execution worker: claim leases, drive instances, heartbeat, recover.

Run one or many; they coordinate purely through Postgres (ADR-0004). A worker
owns no instance permanently — it claims a lease, drives the instance until it
blocks (parked on a retry timer, waiting for approval, done, paused), releases
the lease, and moves on. Another worker (or the same one later) picks up where
it left off from the event log.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from datetime import datetime

from agentforge.config import settings
from agentforge.core.driver import DriveResult, WorkflowDriver
from agentforge.core.escalation import EscalationController
from agentforge.core.leasing import Lease
from agentforge.core.persistence.protocols import LeaseStore
from agentforge.core.ports import SYSTEM_CLOCK, Clock
from agentforge.logging import get_logger

log = get_logger("worker")


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker:
    def __init__(
        self,
        lease_store: LeaseStore,
        driver: WorkflowDriver,
        *,
        worker_id: str | None = None,
        concurrency: int = 8,
        poll_interval_seconds: float = 1.0,
        heartbeat_seconds: float = 10.0,
        recovery_interval_seconds: float = 15.0,
        escalations: EscalationController | None = None,
        escalation_sweep_seconds: float = 20.0,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.worker_id = worker_id or settings.worker_id or default_worker_id()
        self._leases = lease_store
        self._driver = driver
        self._concurrency = concurrency
        self._poll = poll_interval_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._recovery_seconds = recovery_interval_seconds
        self._escalations = escalations
        self._escalation_sweep_seconds = escalation_sweep_seconds
        self._clock = clock

        self._active: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()
        self._wakeup = asyncio.Event()

    # --- lifecycle ---------------------------------------------------
    async def run(self) -> None:
        log.info("worker_start", worker_id=self.worker_id, concurrency=self._concurrency)
        background = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._recovery_loop(), name="recovery"),
        ]
        if self._escalations is not None:
            background.append(
                asyncio.create_task(self._escalation_sweep_loop(), name="escalation-sweep")
            )
        try:
            await self._claim_loop()
        finally:
            self._stopping.set()
            for task in background:
                task.cancel()
            for task in background:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self._drain()
        log.info("worker_stopped", worker_id=self.worker_id)

    def stop(self) -> None:
        self._stopping.set()
        self._wakeup.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(
                NotImplementedError
            ):  # Windows lacks add_signal_handler for SIGTERM
                loop.add_signal_handler(sig, self.stop)

    # --- loops -----------------------------------------------------
    async def _claim_loop(self) -> None:
        while not self._stopping.is_set():
            free = self._concurrency - len(self._active)
            if free > 0:
                try:
                    leases = await self._leases.acquire_runnable(self.worker_id, free, self._now())
                except Exception:
                    log.exception("claim_failed")
                    leases = []
                for lease in leases:
                    if lease.instance_id not in self._active:
                        self._spawn(lease)

            self._wakeup.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._poll)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            held = list(self._active)
            if not held:
                continue
            try:
                alive = await self._leases.heartbeat(self.worker_id, held, self._now())
            except Exception:
                log.exception("heartbeat_failed")
                continue
            for lost in set(held) - alive:
                log.warning("lease_lost_on_heartbeat", instance_id=lost)
                task = self._active.get(lost)
                if task is not None:
                    task.cancel()

    async def _recovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self._recovery_seconds)
            try:
                expired = await self._leases.reclaim_expired(self._now())
            except Exception:
                log.exception("recovery_scan_failed")
                continue
            if expired:
                log.info("expired_leases_available", count=len(expired))
                self._wakeup.set()

    async def _escalation_sweep_loop(self) -> None:
        assert self._escalations is not None
        while True:
            await asyncio.sleep(self._escalation_sweep_seconds)
            try:
                fired = await self._escalations.expire_due(self._now())
            except Exception:
                log.exception("escalation_sweep_failed")
                continue
            if fired:
                log.info("escalations_auto_actioned", count=len(fired))
                self._wakeup.set()  # some instances are RUNNING again

    # --- driving --------------------------------------------------
    def _spawn(self, lease: Lease) -> None:
        task = asyncio.create_task(self._drive_one(lease), name=f"drive:{lease.instance_id}")
        self._active[lease.instance_id] = task
        task.add_done_callback(lambda _t: self._wakeup.set())

    async def _drive_one(self, lease: Lease) -> None:
        guard = self._leases.make_guard(lease)
        try:
            report = await self._driver.drive(lease, guard)
            log.info(
                "drive_done",
                instance_id=lease.instance_id,
                result=report.result.value,
                next_wakeup_at=(
                    report.next_wakeup_at.isoformat() if report.next_wakeup_at else None
                ),
            )
        except asyncio.CancelledError:
            log.warning("drive_cancelled", instance_id=lease.instance_id)
            raise
        except Exception:
            log.exception("drive_error", instance_id=lease.instance_id)
        finally:
            with contextlib.suppress(Exception):
                await self._leases.release(self.worker_id, lease.instance_id)
            self._active.pop(lease.instance_id, None)

    async def _drain(self) -> None:
        if not self._active:
            return
        log.info("draining", in_flight=len(self._active))
        await asyncio.gather(*self._active.values(), return_exceptions=True)

    # --- helpers -------------------------------------------------
    def _now(self) -> datetime:
        return self._clock.now()

    async def run_once(self) -> list[DriveResult]:
        """Claim and drive everything currently runnable, once. For tests."""
        leases = await self._leases.acquire_runnable(self.worker_id, self._concurrency, self._now())
        results: list[DriveResult] = []
        for lease in leases:
            guard = self._leases.make_guard(lease)
            try:
                report = await self._driver.drive(lease, guard)
                results.append(report.result)
            finally:
                await self._leases.release(self.worker_id, lease.instance_id)
        return results
