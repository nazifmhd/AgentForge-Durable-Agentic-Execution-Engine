"""Authentication + authorization for the API.

Two credential types, both resolving to a :class:`Principal` (tenant + scopes):

* ``X-API-Key: af_<key_id>_<secret>`` — looked up by ``sha256(pepper + secret)``.
* ``Authorization: Bearer <jwt>`` — HS256, claims ``{tenant, scopes, exp}``.

Scopes gate write routes; reads require only a valid principal.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge.config import settings
from agentforge.core.persistence.tables import ApiKeyRow
from agentforge.core.ports import SYSTEM_CLOCK, Clock


class Scope(StrEnum):
    WORKFLOWS_READ = "workflows:read"
    WORKFLOWS_WRITE = "workflows:write"
    INSTANCES_READ = "instances:read"
    INSTANCES_WRITE = "instances:write"
    ESCALATIONS_READ = "escalations:read"
    ESCALATIONS_WRITE = "escalations:write"
    DLQ_READ = "dlq:read"
    DLQ_WRITE = "dlq:write"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    tenant_id: str
    scopes: frozenset[str]
    kind: str  # "api_key" | "jwt"

    def has(self, scope: str) -> bool:
        return Scope.ADMIN in self.scopes or scope in self.scopes


class AuthError(Exception):
    """401 — no valid credential."""


class ForbiddenError(Exception):
    """403 — valid credential, missing scope."""


def hash_secret(secret: str) -> str:
    return hashlib.sha256(f"{settings.api_key_pepper}{secret}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    key_id: str
    key_hash: str
    tenant_id: str
    principal_name: str
    scopes: list[str]
    disabled: bool


class ApiKeyStore(Protocol):
    async def get(self, key_id: str) -> ApiKeyRecord | None: ...
    async def create(self, record: ApiKeyRecord) -> None: ...
    async def touch(self, key_id: str, now: datetime) -> None: ...


class PgApiKeyStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def get(self, key_id: str) -> ApiKeyRecord | None:
        async with self._sm() as session:
            row = await session.get(ApiKeyRow, key_id)
        if row is None:
            return None
        return ApiKeyRecord(
            key_id=row.key_id,
            key_hash=row.key_hash,
            tenant_id=row.tenant_id,
            principal_name=row.principal_name,
            scopes=list(row.scopes),
            disabled=row.disabled,
        )

    async def create(self, record: ApiKeyRecord) -> None:
        async with self._sm() as session, session.begin():
            session.add(
                ApiKeyRow(
                    key_id=record.key_id,
                    key_hash=record.key_hash,
                    tenant_id=record.tenant_id,
                    principal_name=record.principal_name,
                    scopes=record.scopes,
                    disabled=record.disabled,
                )
            )

    async def touch(self, key_id: str, now: datetime) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(ApiKeyRow).where(ApiKeyRow.key_id == key_id).values(last_used_at=now)
            )

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeyRecord]:
        async with self._sm() as session:
            rows = await session.scalars(select(ApiKeyRow).where(ApiKeyRow.tenant_id == tenant_id))
            return [
                ApiKeyRecord(
                    key_id=r.key_id,
                    key_hash=r.key_hash,
                    tenant_id=r.tenant_id,
                    principal_name=r.principal_name,
                    scopes=list(r.scopes),
                    disabled=r.disabled,
                )
                for r in rows
            ]


def mint_api_key(
    *,
    tenant_id: str,
    name: str,
    scopes: list[str],
    key_id: str | None = None,
) -> tuple[str, ApiKeyRecord]:
    """Returns ``(plaintext_key, record_to_store)``. The plaintext is shown once."""
    key_id = key_id or secrets.token_hex(8)
    secret = secrets.token_urlsafe(24)
    plaintext = f"af_{key_id}_{secret}"
    record = ApiKeyRecord(
        key_id=key_id,
        key_hash=hash_secret(secret),
        tenant_id=tenant_id,
        principal_name=name,
        scopes=scopes,
        disabled=False,
    )
    return plaintext, record


class AuthService:
    def __init__(self, api_keys: ApiKeyStore, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self._keys = api_keys
        self._clock = clock

    async def authenticate(self, *, api_key: str | None, bearer: str | None) -> Principal:
        if api_key:
            return await self._from_api_key(api_key)
        if bearer:
            return self._from_jwt(bearer)
        raise AuthError("no credentials")

    async def _from_api_key(self, raw: str) -> Principal:
        prefix, _, rest = raw.partition("_")
        key_id, _, secret = rest.partition("_")
        if prefix != "af" or not key_id or not secret:
            raise AuthError("malformed API key")
        record = await self._keys.get(key_id)
        if record is None or record.disabled:
            raise AuthError("unknown API key")
        if not secrets.compare_digest(record.key_hash, hash_secret(secret)):
            raise AuthError("bad API key")
        await self._keys.touch(key_id, self._clock.now())
        return Principal(
            name=record.principal_name,
            tenant_id=record.tenant_id,
            scopes=frozenset(record.scopes),
            kind="api_key",
        )

    def _from_jwt(self, token: str) -> Principal:
        try:
            claims: dict[str, Any] = jwt.decode(
                token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {exc}") from exc
        tenant = claims.get("tenant")
        if not tenant:
            raise AuthError("token has no tenant claim")
        scopes = claims.get("scopes") or []
        return Principal(
            name=str(claims.get("sub", "jwt-user")),
            tenant_id=str(tenant),
            scopes=frozenset(scopes),
            kind="jwt",
        )
