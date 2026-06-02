"""Discord webhook notification channel.

Posts to a Discord channel via an Incoming Webhook URL. Designed for
community / homelab deployments where Slack would be overkill — same
chat-style notification surface, no bot user to invite, no workspace
admin to ask. Discord webhooks are per-channel and free.

Payload shape:

    {"content": "<emoji> <message>", "username": "...", "avatar_url": "..."}

  * `content` carries the rendered notification. Discord caps it at
    2000 chars — we truncate ourselves so the channel stays well-
    behaved against pathologically long pipeline errors.
  * `username` and `avatar_url` are optional per-message overrides;
    when set they replace the defaults baked into the webhook by
    whoever created it in the Discord UI. Useful when one Discord
    server hosts notifications from multiple deployments (per-env or
    per-tenant identity).
  * `thread_id` is an optional Discord thread snowflake. When set,
    messages land inside that specific thread instead of the
    channel's main feed (the webhook URL gains a `?thread_id=<id>`
    query parameter on each POST).

Unicode emojis (✅ ⚠️ ❌) for severity — Discord renders shortcodes
inconsistently (`:warning:` becomes the text literal in most embeds),
so the unicode chars are the safe choice.

URL comes from the `iac-cartographer/discord` secret as
`{"url": "https://discord.com/api/webhooks/.../..."}` — the URL
embeds the webhook ID + token, so the URL IS the credential.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel

if TYPE_CHECKING:
    from iac_cartographer.models import DiscordCredentials

logger = logging.getLogger("iac_cartographer.notifications.discord")

DEFAULT_TIMEOUT_S = 15.0
# Discord caps webhook `content` at 2000 chars. We pre-truncate so a
# misbehaving pipeline message doesn't cause a 400 on send.
_MAX_CONTENT_CHARS = 2000

# Same unicode-emoji convention the Teams channel uses (Discord
# renders shortcodes inconsistently in webhook content).
_PREFIXES: dict[NotificationLevel, str] = {
    NotificationLevel.INFO: "✅",
    NotificationLevel.WARN: "⚠️",
    NotificationLevel.ERROR: "❌",
}


class DiscordChannel(NotificationChannel):
    """POST `{"content": "..."}` payloads to a Discord webhook URL."""

    name = "discord"

    def __init__(
        self,
        creds: DiscordCredentials,
        *,
        username: str | None = None,
        avatar_url: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        self._url = creds.url
        self._username = username
        self._avatar_url = avatar_url
        # When set, append `?thread_id=<id>` to the POST URL so the
        # message lands inside a specific thread rather than the
        # channel's main feed. Discord's webhook API treats this as a
        # query parameter (not a payload field). Routed via httpx
        # `params={...}` so URL encoding is handled correctly even if
        # the operator passes a numeric snowflake ID as a string with
        # whitespace or other quoting accidents.
        self._thread_id = thread_id
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_S,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def notify(self, level: NotificationLevel, message: str) -> None:
        content = f"{_PREFIXES[level]} {message}"
        if len(content) > _MAX_CONTENT_CHARS:
            # Truncate with a marker so operators reading Discord see
            # the message was cut, not just mysteriously short.
            content = content[: _MAX_CONTENT_CHARS - 1] + "…"

        payload: dict[str, object] = {"content": content}
        if self._username is not None:
            payload["username"] = self._username
        if self._avatar_url is not None:
            payload["avatar_url"] = self._avatar_url

        try:
            client = self._ensure_client()
            params = {"thread_id": self._thread_id} if self._thread_id else None
            resp = await client.post(self._url, json=payload, params=params)
            # Discord returns 204 No Content on success. Anything 4xx
            # is a bad webhook URL / malformed payload / rate limit.
            if resp.status_code >= 400:
                logger.warning(
                    "discord: %s post failed (status=%d, body=%s)",
                    level.value,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.warning("discord: %s post raised", level.value, exc_info=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
