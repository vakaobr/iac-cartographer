"""Microsoft Teams notification channel (Adaptive Card via webhook).

Microsoft is deprecating the legacy "Office 365 Connector" webhooks in
favour of **Workflow webhooks** (Power Automate). Both accept the same
JSON envelope — an Adaptive Card v1.4 inside an attachment — so this
channel works with either. The shape:

    {
      "type": "message",
      "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {
          "type": "AdaptiveCard",
          "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
          "version": "1.4",
          "body": [
            {"type": "TextBlock", "text": "...", ...}
          ]
        }
      }]
    }

Severity → Adaptive Card colour mapping:

  info  → "good"      (greenish)
  warn  → "warning"   (amber)
  error → "attention" (red)

Unicode emojis are used in the header text rather than Slack-style
`:emoji:` shortcodes — Teams does NOT render the shortcode form.

URL comes from the `iac-cartographer/teams` secret as
`{"url": "https://prod-XX.westeurope.logic.azure.com:443/..."}` —
the workflow URL embeds a SAS token, so never check it into
version-controlled config.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel

if TYPE_CHECKING:
    from iac_cartographer.models import TeamsCredentials

logger = logging.getLogger("iac_cartographer.notifications.teams")

DEFAULT_TIMEOUT_S = 15.0

# Unicode emoji + Adaptive Card colour per severity. Emojis go in the
# TextBlock text; colour goes in the colour field of the same block.
_LEVEL_META: dict[NotificationLevel, tuple[str, str]] = {
    NotificationLevel.INFO: ("✅", "good"),  # ✅
    NotificationLevel.WARN: ("⚠️", "warning"),  # ⚠️
    NotificationLevel.ERROR: ("❌", "attention"),  # ❌
}


def _build_adaptive_card(level: NotificationLevel, message: str) -> dict[str, object]:
    """Compose the Adaptive Card envelope for one notification.

    Kept as a module function so tests can assert the structure
    without going through an httpx mock.
    """
    emoji, colour = _LEVEL_META[level]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"{emoji} {message}",
                            "weight": "Bolder",
                            "color": colour,
                            "wrap": True,
                        }
                    ],
                },
            }
        ],
    }


class TeamsChannel(NotificationChannel):
    """POST Adaptive Card payloads to a Teams workflow webhook URL."""

    name = "teams"

    def __init__(self, creds: TeamsCredentials) -> None:
        self._url = creds.url
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_S,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        return self._client

    async def notify(self, level: NotificationLevel, message: str) -> None:
        payload = _build_adaptive_card(level, message)
        try:
            client = self._ensure_client()
            resp = await client.post(self._url, json=payload)
            # Teams workflow webhooks return 200/202 on success; anything
            # 4xx/5xx (auth issue, malformed card) is a real failure.
            if resp.status_code >= 400:
                logger.warning(
                    "teams: %s post failed (status=%d, body=%s)",
                    level.value,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.warning("teams: %s post raised", level.value, exc_info=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
