"""Multi-channel fanout for pipeline notifications.

The dispatcher owns N `(channel, allowed_levels)` pairs and exposes the
same `info / warn / error / close` surface the old single-Slack notifier
had — so call sites in `cli.py` don't change shape when the deployment
goes from "Slack only" to "Slack + email + Teams".

Behaviour:

  * **Concurrent fanout** — `asyncio.gather(..., return_exceptions=True)`
    so a slow Teams webhook doesn't block a fast Slack one.
  * **Level filter** — each channel carries its own
    `set[NotificationLevel]`; the dispatcher drops disallowed levels
    before calling `notify()`. Typical use:
      - chat → all three
      - PagerDuty / email → errors only
  * **Per-channel failure isolation** — gather collects exceptions
    instead of propagating. We log them with the channel name so
    operators see *which* destination is broken.
  * **Empty dispatcher is legal** — `notifications: []` (and no
    legacy Slack fallback) means "do not notify anywhere". Useful for
    `--dry-run` and CI smoke tests. Every method becomes a no-op.

The dispatcher is constructed at CLI startup via
`iac_cartographer.notifications.build_dispatcher` and lives for the
duration of the run. It does not own any channel-specific config beyond
the allowed-levels filter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from iac_cartographer.notifications.base import NotificationLevel

if TYPE_CHECKING:
    from iac_cartographer.notifications.base import NotificationChannel

logger = logging.getLogger("iac_cartographer.notifications")


class NotificationDispatcher:
    """Fans `info / warn / error` calls out to N channels concurrently."""

    def __init__(self, channels: list[tuple[NotificationChannel, set[NotificationLevel]]]) -> None:
        self._channels = channels

    async def info(self, message: str) -> None:
        await self._fanout(NotificationLevel.INFO, message)

    async def warn(self, message: str) -> None:
        await self._fanout(NotificationLevel.WARN, message)

    async def error(self, message: str) -> None:
        await self._fanout(NotificationLevel.ERROR, message)

    async def close(self) -> None:
        """Close every channel's open client. Safe to call multiple times."""
        results = await asyncio.gather(
            *(channel.close() for channel, _ in self._channels),
            return_exceptions=True,
        )
        for (channel, _), result in zip(self._channels, results, strict=True):
            if isinstance(result, BaseException):
                logger.debug("notifier %s: close raised", channel.name, exc_info=result)

    async def _fanout(self, level: NotificationLevel, message: str) -> None:
        eligible = [(channel, allowed) for channel, allowed in self._channels if level in allowed]
        if not eligible:
            return
        results = await asyncio.gather(
            *(channel.notify(level, message) for channel, _ in eligible),
            return_exceptions=True,
        )
        for (channel, _), result in zip(eligible, results, strict=True):
            if isinstance(result, BaseException):
                # Channels SHOULD log + swallow their own errors; this is
                # the defence-in-depth log line that fires when a channel
                # leaks an exception out anyway.
                logger.warning(
                    "notifier %s: %s post raised",
                    channel.name,
                    level.value,
                    exc_info=result,
                )
