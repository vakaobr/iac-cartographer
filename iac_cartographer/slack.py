"""Slack notifier — info / warn / error to the configured channel.

Best-effort: every method swallows transport failures and logs the error
rather than raising. The pipeline run must not fail just because Slack is
unreachable; the failure already shows up in your structured logs.

Posts via `chat.postMessage` with a bot token. Channel can be either an ID
(`C0...`) or a `#name`-prefixed string — both work with the bot token.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from iac_cartographer.models import SlackCredentials

logger = logging.getLogger("iac_cartographer.slack")

SLACK_BASE_URL = "https://slack.com/api"
DEFAULT_TIMEOUT_S = 15.0

# Single-character emoji prefixes for the three severity levels — terse so
# the message text gets the screen real estate.
_PREFIXES = {
    "info": ":white_check_mark:",
    "warn": ":warning:",
    "error": ":x:",
}


class SlackNotifier:
    def __init__(self, creds: SlackCredentials, *, channel: str | None = None) -> None:
        """`channel` overrides `creds.channel_id` if provided (used when the
        config wants a different channel from the bot's default).

        The underlying `httpx.AsyncClient` is constructed lazily on first post,
        so a notifier created but never used (e.g. on a `--dry-run` path that
        bails out before any message is sent) doesn't allocate a connection
        pool. `close()` is similarly a no-op if no client was opened.
        """
        self._token = creds.bot_token
        self._channel = channel or creds.channel_id
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=SLACK_BASE_URL,
                timeout=DEFAULT_TIMEOUT_S,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        return self._client

    async def info(self, message: str) -> None:
        await self._post("info", message)

    async def warn(self, message: str) -> None:
        await self._post("warn", message)

    async def error(self, message: str) -> None:
        await self._post("error", message)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, level: str, message: str) -> None:
        text = f"{_PREFIXES[level]} {message}"
        try:
            client = self._ensure_client()
            resp = await client.post(
                "/chat.postMessage",
                json={"channel": self._channel, "text": text},
            )
            payload = resp.json() if resp.status_code == 200 else {}
            if resp.status_code != 200 or not payload.get("ok", False):
                # Slack returns 200 with `ok: false` for most failures; status >= 400
                # is rarer (network / auth-shape).
                logger.warning(
                    "slack: %s post failed (status=%d, ok=%s, error=%s)",
                    level,
                    resp.status_code,
                    payload.get("ok"),
                    payload.get("error"),
                )
        except Exception:
            logger.warning("slack: %s post raised", level, exc_info=True)
