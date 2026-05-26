"""Slack-compatible incoming-webhook channel.

Posts a Slack-shaped JSON document (`{"text": "..."}`) to a webhook URL.
That payload format is the de-facto interoperability standard for chat
platforms that want zero-friction Slack tooling re-use, so this single
channel covers three destinations at once:

  * **Slack incoming webhooks** — the URL-based posting path Slack
    supports alongside the bot-token API. Useful when you don't want
    to run a bot user.
  * **RocketChat** — accepts Slack-shaped payloads natively at any
    webhook URL.
  * **Mattermost** — same. Self-hosted Slack-alternative used in
    regulated / on-prem environments.

The bot-token Slack channel (`iac_cartographer.notifications.slack`)
stays as-is — operators with a Slack bot user keep using it. This
channel is for the webhook-URL flavour.

URL comes from the `iac-cartographer/slack_webhook` secret as
`{"url": "https://hooks.slack.com/services/..."}` — never check the
URL into version-controlled config (the URL IS the credential).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel

if TYPE_CHECKING:
    from iac_cartographer.models import SlackWebhookCredentials

logger = logging.getLogger("iac_cartographer.notifications.slack_webhook")

DEFAULT_TIMEOUT_S = 15.0

# Same emoji prefixes the bot-token Slack channel uses — chats look
# identical regardless of which Slack transport is in play.
_PREFIXES: dict[NotificationLevel, str] = {
    NotificationLevel.INFO: ":white_check_mark:",
    NotificationLevel.WARN: ":warning:",
    NotificationLevel.ERROR: ":x:",
}


class SlackWebhookChannel(NotificationChannel):
    """POST Slack-shaped `{"text": "..."}` payloads to a webhook URL."""

    name = "slack_webhook"

    def __init__(self, creds: SlackWebhookCredentials) -> None:
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
        text = f"{_PREFIXES[level]} {message}"
        try:
            client = self._ensure_client()
            resp = await client.post(self._url, json={"text": text})
            # Slack-compat endpoints return 200 with a body of "ok" (Slack)
            # or `{"success": true}` (RocketChat / Mattermost). Any status
            # >= 400 is a transport-level failure worth logging.
            if resp.status_code >= 400:
                logger.warning(
                    "slack_webhook: %s post failed (status=%d, body=%s)",
                    level.value,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.warning("slack_webhook: %s post raised", level.value, exc_info=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
