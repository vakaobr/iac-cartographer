"""NotificationChannel ABC + severity enum.

A `NotificationChannel` posts pipeline events (start, per-repo warn, end,
error) to ONE destination — Slack workspace, Teams channel, email
recipient list, etc. Multiple channels run side-by-side via the
`NotificationDispatcher`, which fans every event out concurrently and
isolates per-channel failures (one broken webhook does NOT sink the run).

The Slack-only era only had one notifier; this ABC matches that surface
so call sites stay terse:

  await notifier.info("iac-cartographer: run starting")
  await notifier.warn("AI-H1 — possible prompt injection in acme/foo")
  await notifier.error("preflight failed — Confluence unreachable")

Channels are expected to:

  * **Swallow transport errors** — log + return; never raise. The
    dispatcher does its own try/except as defence in depth, but each
    channel owning its own error handling keeps log lines specific
    ("slack: post failed" beats "channel post failed").
  * **Be safe to construct lazily** — no network on `__init__`. The
    typical lifecycle is `build → notify*N → close`; a channel that
    never receives a `notify()` call (e.g. on a `--dry-run` that bails
    early) should not allocate connections.
  * **Honour their own per-level filter** — but the dispatcher already
    drops disallowed levels before calling `notify()`, so channels can
    assume they will only be invoked at permitted severities.

Add a new channel by subclassing `NotificationChannel`, registering a
config kind in `iac_cartographer.models`, and adding a branch to
`iac_cartographer.notifications.build_dispatcher`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum


class NotificationLevel(StrEnum):
    """Three-level severity matching the pre-multichannel Slack API.

    Kept as a `StrEnum` so the YAML config can write `"info"` /
    `"warn"` / `"error"` literals and the dispatcher can compare them
    against a `set[NotificationLevel]` without manual casting.
    """

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class NotificationChannel(ABC):
    """One destination — Slack workspace, Teams channel, email list, etc."""

    #: Short human-readable label used in log lines. Subclasses MUST set
    #: this (e.g. `"slack"`, `"teams"`, `"email"`). Showing up as
    #: `"notifier ?: ..."` in operator logs is a bug.
    name: str = "?"

    @abstractmethod
    async def notify(self, level: NotificationLevel, message: str) -> None:
        """Best-effort post. Implementations SHOULD log + swallow
        transport errors rather than raising.

        The dispatcher pre-filters by level before calling this, so the
        channel doesn't need to re-check `level` against its own filter.
        """

    async def close(self) -> None:
        """Release any pooled connections / open clients.

        Default implementation is a no-op for channels that don't need
        cleanup (stdout / file). Channels that own an `httpx.AsyncClient`,
        SMTP connection pool, etc. should override.
        """
        return
