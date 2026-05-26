"""Generic webhook notification channel.

Posts a JSON document to an arbitrary URL — the catch-all destination
for anything that doesn't fit one of the dedicated channels (Slack,
Teams, email, …). Useful for:

  * Internal observability platforms that ingest JSON events.
  * Custom Lambda / Cloud Function endpoints that post to multiple
    downstream destinations (chat + ticket + DB) from one place.
  * Forwarding to OpsGenie / PagerDuty / Splunk via their generic-event
    intake URLs.

Payload schema (stable — change-detect via the schema version field):

    {
      "schema": "iac-cartographer.notification.v1",
      "level": "info" | "warn" | "error",
      "message": "...",
      "ts": "2026-05-26T10:30:00Z",
      "source": "iac-cartographer"
    }

URL comes from the `iac-cartographer/webhook` secret as
`{"url": "https://..."}` — webhook URLs typically embed a secret token
in the URL itself so they should never live in plain YAML.

Optional bearer-token auth via `extra_headers` (in the config block,
not the secret) for endpoints that want a separate `Authorization`
header on top of the URL-embedded secret.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel

if TYPE_CHECKING:
    from iac_cartographer.models import WebhookCredentials

logger = logging.getLogger("iac_cartographer.notifications.webhook")

DEFAULT_TIMEOUT_S = 15.0
PAYLOAD_SCHEMA = "iac-cartographer.notification.v1"


class GenericWebhookChannel(NotificationChannel):
    """POST JSON-shaped notifications to an arbitrary URL."""

    name = "webhook"

    def __init__(
        self,
        creds: WebhookCredentials,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._url = creds.url
        self._extra_headers = dict(extra_headers or {})
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_S,
                headers={"Content-Type": "application/json", **self._extra_headers},
            )
        return self._client

    async def notify(self, level: NotificationLevel, message: str) -> None:
        payload = {
            "schema": PAYLOAD_SCHEMA,
            "level": level.value,
            "message": message,
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source": "iac-cartographer",
        }
        try:
            client = self._ensure_client()
            resp = await client.post(self._url, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "webhook: %s post failed (status=%d, body=%s)",
                    level.value,
                    resp.status_code,
                    # Truncate any error body so we don't dump huge HTML
                    # error pages into operator logs.
                    resp.text[:200],
                )
        except Exception:
            logger.warning("webhook: %s post raised", level.value, exc_info=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
