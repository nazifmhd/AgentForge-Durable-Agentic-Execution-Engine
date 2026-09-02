"""Notification channels for human-in-the-loop (escalations, completions).

``Notifier`` is best-effort by contract — a failed notification is logged, never
raised, and never blocks a workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from agentforge.logging import get_logger

log = get_logger("notify")


@dataclass(frozen=True, slots=True)
class Notification:
    channel: str
    subject: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Notifier(Protocol):
    async def notify(self, note: Notification) -> None: ...


class LogNotifier:
    async def notify(self, note: Notification) -> None:
        log.info(
            "notification",
            channel=note.channel,
            subject=note.subject,
            body=note.body,
            **note.metadata,
        )


class WebhookNotifier:
    """POSTs the notification as JSON — point it at a Slack / n8n / Teams webhook."""

    def __init__(
        self,
        endpoints: dict[str, str],
        *,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoints = endpoints
        self._timeout = timeout_seconds
        self._client = client

    async def notify(self, note: Notification) -> None:
        url = self._endpoints.get(note.channel)
        if url is None:
            log.warning("no_webhook_for_channel", channel=note.channel)
            return
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.post(
                url,
                json={
                    "channel": note.channel,
                    "subject": note.subject,
                    "body": note.body,
                    "metadata": note.metadata,
                },
            )
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 - notifications are best effort
            log.warning("notify_failed", channel=note.channel)
        finally:
            if self._client is None:
                await client.aclose()


class MultiNotifier:
    """Fan a notification out to several notifiers; one failing doesn't stop the rest."""

    def __init__(self, *notifiers: Notifier) -> None:
        self._notifiers = notifiers

    async def notify(self, note: Notification) -> None:
        for n in self._notifiers:
            try:
                await n.notify(note)
            except Exception:  # noqa: BLE001
                log.warning("sub_notifier_failed", notifier=type(n).__name__)


__all__ = [
    "LogNotifier",
    "MultiNotifier",
    "Notification",
    "Notifier",
    "WebhookNotifier",
]
