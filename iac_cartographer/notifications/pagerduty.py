"""PagerDuty Events API v2 notification channel.

Triggers PagerDuty incidents via the public events intake endpoint.
The Events API is the *de jure* path for service-to-PagerDuty
notifications — separate from the user-facing REST API and tied to a
**routing key** (per-service integration key) rather than user / app
credentials.

Routing model:
  * Each Service in PagerDuty owns one (or more) Events API v2
    Integrations, each with its own routing key.
  * The routing key alone identifies the destination Service and the
    escalation policy attached to it — so this channel's only
    "credential" is the routing key itself.

This channel sends `event_action: "trigger"` events only — we don't
yet model ack / resolve flows from the inventory pipeline. Operators
who want auto-resolve on a follow-up green run can build that on top
once we surface a `dedup_key` field.

Severity mapping (level → PagerDuty severity):
  * info  → "info"
  * warn  → "warning"
  * error → "error"

PagerDuty itself recognises `critical / error / warning / info` — we
pick "error" rather than "critical" because the channel framework
already supports per-entry filtering; if you want page-on-error,
narrow via `levels: [error]` rather than escalating every notify to
critical.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel

if TYPE_CHECKING:
    from iac_cartographer.models import PagerDutyCredentials

logger = logging.getLogger("iac_cartographer.notifications.pagerduty")

EVENTS_API_URL = "https://events.pagerduty.com/v2/enqueue"
DEFAULT_TIMEOUT_S = 15.0

# Level → PagerDuty `severity` field. The Events API enum is
# {"critical", "error", "warning", "info"}; we deliberately map
# our highest level to "error" not "critical" so callers retain
# control via the per-entry `levels:` filter (narrow to [error] for
# page-on-error rather than escalating semantically).
_LEVEL_TO_SEVERITY: dict[NotificationLevel, str] = {
    NotificationLevel.INFO: "info",
    NotificationLevel.WARN: "warning",
    NotificationLevel.ERROR: "error",
}


def _build_event(
    *,
    routing_key: str,
    level: NotificationLevel,
    message: str,
) -> dict[str, object]:
    """Compose the PagerDuty Events API v2 payload.

    Pulled out as a pure function so tests can assert the shape
    without an httpx round-trip. The `summary` field is capped at
    1024 chars per the API contract — pipeline messages are
    almost always under that, but the truncation keeps the channel
    well-behaved against pathologically int error strings.
    """
    summary = message[:1024]
    return {
        "routing_key": routing_key,
        "event_action": "trigger",
        "payload": {
            "summary": summary,
            "severity": _LEVEL_TO_SEVERITY[level],
            "source": "iac-cartographer",
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    }


class PagerDutyChannel(NotificationChannel):
    """Trigger PagerDuty incidents via Events API v2."""

    name = "pagerduty"

    def __init__(self, creds: PagerDutyCredentials) -> None:
        self._routing_key = creds.routing_key
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_S,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def notify(self, level: NotificationLevel, message: str) -> None:
        body = _build_event(
            routing_key=self._routing_key,
            level=level,
            message=message,
        )
        try:
            client = self._ensure_client()
            resp = await client.post(EVENTS_API_URL, json=body)
            # Events API returns 202 Accepted on success. 4xx = bad
            # routing key / malformed payload; 5xx = PagerDuty
            # incident on their side (rare, but possible).
            if resp.status_code >= 400:
                logger.warning(
                    "pagerduty: %s post failed (status=%d, body=%s)",
                    level.value,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.warning("pagerduty: %s post raised", level.value, exc_info=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
