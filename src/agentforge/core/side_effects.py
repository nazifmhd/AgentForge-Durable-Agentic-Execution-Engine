"""``SideEffectGuard`` — the exactly-once boundary for external actions (ADR-0003).

Flow per effect:

1. **claim** — atomic upsert into the outbox by a deterministic idempotency key
   (``instance_id : step_id : effect_name : params``). The returned row tells us
   whether this is a fresh attempt or a resumed one, and whether it already ran.
2. **execute** — call the provider. Prefer the provider's own idempotency key;
   for providers without one, ``reconcile`` first on a resumed attempt to see if
   the previous try actually landed.
3. **record** — mark the row ``EXECUTED`` with the result.

A crash between (1) and (3) leaves a ``PENDING`` row; the step re-runs on
recovery, re-enters here, and step 2's idempotency/reconcile keeps the external
world consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentforge.core.hashing import digest
from agentforge.core.outbox import OutboxEntry, OutboxStore
from agentforge.core.ports import SYSTEM_CLOCK, Clock
from agentforge.exceptions import CompensationError, SideEffectError
from agentforge.integrations.actions.base import (
    EffectRequest,
    EffectResult,
    ProviderRegistry,
)
from agentforge.logging import get_logger

log = get_logger("side_effects")


class EffectStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    FAILED = "failed"
    COMPENSATED = "compensated"


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    idempotency_key: str
    effect_name: str
    step_id: str
    params: dict[str, Any]
    result: EffectResult
    deduplicated: bool
    guarantee: str


class SideEffectGuard:
    def __init__(
        self,
        outbox: OutboxStore,
        providers: ProviderRegistry,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._outbox = outbox
        self._providers = providers
        self._clock = clock

    @staticmethod
    def idempotency_key(
        instance_id: str, step_id: str, effect_name: str, params: dict[str, Any]
    ) -> str:
        return digest(
            {
                "instance_id": instance_id,
                "step_id": step_id,
                "effect_name": effect_name,
                "params": params,
            }
        )

    async def execute(
        self,
        *,
        instance_id: str,
        tenant_id: str,
        step_id: str,
        effect_name: str,
        params: dict[str, Any],
        provider_name: str = "noop",
    ) -> EffectOutcome:
        provider = self._providers.get(provider_name)
        key = self.idempotency_key(instance_id, step_id, effect_name, params)
        supports_key = provider.supports_idempotency_key(effect_name)
        guarantee = "exactly_once" if supports_key else "at_least_once_dedup"
        req = EffectRequest(
            effect_name=effect_name,
            params=params,
            idempotency_key=key,
            instance_id=instance_id,
            tenant_id=tenant_id,
            step_id=step_id,
        )

        row = await self._outbox.claim(
            OutboxEntry(
                idempotency_key=key,
                instance_id=instance_id,
                tenant_id=tenant_id,
                step_id=step_id,
                effect_name=effect_name,
                provider=provider_name,
                params=params,
                guarantee=guarantee,
            )
        )

        if row.status == EffectStatus.EXECUTED:
            return EffectOutcome(
                idempotency_key=key,
                effect_name=effect_name,
                step_id=step_id,
                params=params,
                result=EffectResult(ok=True, data=row.result or {}, provider_ref=row.provider_ref),
                deduplicated=True,
                guarantee=row.guarantee,
            )
        if row.status == EffectStatus.COMPENSATED:
            raise SideEffectError(f"effect {effect_name} ({key}) was already compensated")

        resuming = row.attempts > 1

        result: EffectResult | None = None
        try:
            if resuming and not supports_key:
                result = await provider.reconcile(req)
                if result is not None:
                    log.info("effect_reconciled", effect=effect_name, key=key)
            if result is None:
                result = await provider.execute(req)
        except Exception as exc:
            await self._outbox.mark(
                key,
                status=EffectStatus.FAILED,
                error=str(exc),
                updated_at=self._clock.now(),
            )
            raise SideEffectError(f"effect {effect_name} raised: {exc}") from exc

        if not result.ok:
            await self._outbox.mark(
                key,
                status=EffectStatus.FAILED,
                error=result.error,
                updated_at=self._clock.now(),
            )
            raise SideEffectError(f"effect {effect_name} returned not-ok: {result.error}")

        await self._outbox.mark(
            key,
            status=EffectStatus.EXECUTED,
            result=result.data,
            provider_ref=result.provider_ref,
            error=None,
            updated_at=self._clock.now(),
        )
        return EffectOutcome(
            idempotency_key=key,
            effect_name=effect_name,
            step_id=step_id,
            params=params,
            result=result,
            deduplicated=False,
            guarantee=guarantee,
        )

    async def compensate_instance(self, instance_id: str, tenant_id: str) -> list[EffectOutcome]:
        """Undo every executed effect for the instance, most-recent first."""
        rows = await self._outbox.list_for_instance(
            instance_id, tenant_id, status=EffectStatus.EXECUTED
        )
        undone: list[EffectOutcome] = []
        for row in reversed(rows):
            provider = self._providers.get(row.provider)
            req = EffectRequest(
                effect_name=row.effect_name,
                params=row.params,
                idempotency_key=row.idempotency_key,
                instance_id=instance_id,
                tenant_id=tenant_id,
                step_id=row.step_id,
            )
            executed = EffectResult(ok=True, data=row.result or {}, provider_ref=row.provider_ref)
            try:
                comp = await provider.compensate(req, executed)
            except Exception as exc:
                await self._outbox.mark(
                    row.idempotency_key,
                    compensation_status="failed",
                    error=str(exc),
                    updated_at=self._clock.now(),
                )
                raise CompensationError(
                    f"compensation for {row.effect_name} failed: {exc}"
                ) from exc
            await self._outbox.mark(
                row.idempotency_key,
                status=EffectStatus.COMPENSATED,
                compensation_status="done",
                updated_at=self._clock.now(),
            )
            undone.append(
                EffectOutcome(
                    idempotency_key=row.idempotency_key,
                    effect_name=row.effect_name,
                    step_id=row.step_id,
                    params=row.params,
                    result=comp or EffectResult(ok=True),
                    deduplicated=False,
                    guarantee=row.guarantee,
                )
            )
        return undone

    async def list_effects(self, instance_id: str, tenant_id: str) -> list[OutboxEntry]:
        return await self._outbox.list_for_instance(instance_id, tenant_id)
