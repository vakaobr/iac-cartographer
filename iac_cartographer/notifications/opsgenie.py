"""Opsgenie Alerts API notification channel.

Creates Opsgenie alerts via the public Alerts API. Authenticates with
a team / integration **API key** in an `Authorization: GenieKey <key>`
header (the canonical scheme — distinct from regular bearer tokens).

Region split — Opsgenie maintains two independent control planes:

  * **US** (default) — `https://api.opsgenie.com`
  * **EU**           — `https://api.eu.opsgenie.com`

The two are NOT linked; an API key issued on one plane will be
rejected by the other. The channel takes a `region` config field
(`"us"` or `"eu"`) so EU-resident customers land on the right host.

Severity mapping (level → Opsgenie priority):
  * info  → "P5"
  * warn  → "P3"
  * error → "P1"

This matches the typical operator convention — P1 pages on-call, P5
just lands silently in the alert queue. Operators who want page-only
behaviour should narrow with `levels: [error]` rather than reshuffling
the priority map.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

import httpx

from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel

if TYPE_CHECKING:
    from iac_cartographer.models import OpsgenieCredentials

logger = logging.getLogger("iac_cartographer.notifications.opsgenie")

DEFAULT_TIMEOUT_S = 15.0

# Region → API host map. Stored at module level so tests can patch
# the dict if/when Opsgenie adds another region (none planned, but
# defensive).
_REGION_HOSTS: dict[str, str] = {
    "us": "https://api.opsgenie.com",
    "eu": "https://api.eu.opsgenie.com",
}

# Level → Opsgenie priority. Opsgenie's API accepts P1-P5; we map
# error to P1 so the default config gets the operator's attention
# but leave info / warn at lower priorities so non-error notifications
# don't page.
_LEVEL_TO_PRIORITY: dict[NotificationLevel, str] = {
    NotificationLevel.INFO: "P5",
    NotificationLevel.WARN: "P3",
    NotificationLevel.ERROR: "P1",
}


def _build_alert(
    *,
    level: NotificationLevel,
    message: str,
) -> dict[str, object]:
    """Compose the Opsgenie Alerts API payload.

    Pure function so tests can assert without an httpx round-trip.
    `message` is capped at 130 chars (Opsgenie API hard limit); the
    full message lands in `description` (15000-char limit, more than
    we'll ever produce).
    """
    return {
        "message": message[:130],
        "description": message,
        "priority": _LEVEL_TO_PRIORITY[level],
        "source": "iac-cartographer",
        "details": {
            "level": level.value,
        },
    }


class OpsgenieChannel(NotificationChannel):
    """Create Opsgenie alerts via the public Alerts API."""

    name = "opsgenie"

    def __init__(
        self,
        creds: OpsgenieCredentials,
        *,
        region: Literal["us", "eu"] = "us",
    ) -> None:
        self._api_key = creds.api_key
        self._host = _REGION_HOSTS[region]
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_S,
                headers={
                    # `GenieKey` is the Opsgenie-specific scheme — NOT
                    # the standard `Bearer` token format.
                    "Authorization": f"GenieKey {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def notify(self, level: NotificationLevel, message: str) -> None:
        body = _build_alert(level=level, message=message)
        try:
            client = self._ensure_client()
            resp = await client.post(f"{self._host}/v2/alerts", json=body)
            # Opsgenie returns 202 Accepted on success (alert queued).
            # 401 = wrong region / bad key; 422 = schema mismatch.
            if resp.status_code >= 400:
                logger.warning(
                    "opsgenie: %s post failed (status=%d, body=%s)",
                    level.value,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.warning("opsgenie: %s post raised", level.value, exc_info=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
