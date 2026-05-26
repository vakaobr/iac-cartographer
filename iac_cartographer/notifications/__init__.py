"""Notifications — pluggable destinations for pipeline events.

Public surface:

  * `NotificationChannel`        — ABC every channel extends.
  * `NotificationLevel`          — `info / warn / error` enum.
  * `NotificationDispatcher`     — multi-channel fanout with per-level
                                   filter + per-channel error isolation.
  * `SlackChannel`               — first concrete channel (Slack
                                   `chat.postMessage` via bot token).
  * `build_dispatcher`           — factory that wires a list of
                                   channel configs into a dispatcher,
                                   with a back-compat shim for the
                                   legacy single-Slack `slack:` block.

Adding a new channel: subclass `NotificationChannel`, register a config
kind in `iac_cartographer.models` (extend `NotificationConfig` union),
and add a branch to `build_dispatcher`. The dispatcher contract is
intentionally narrow (`notify(level, message)`) so adapters for very
different upstreams (chat / email / pager / SNS) can implement it
without dragging in adapter-specific concepts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from iac_cartographer.constants import ConfigError
from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel
from iac_cartographer.notifications.dispatcher import NotificationDispatcher
from iac_cartographer.notifications.slack import SlackChannel

if TYPE_CHECKING:
    from iac_cartographer.models import (
        AppConfig,
        SlackCredentials,
    )


def build_dispatcher(
    config: AppConfig,
    *,
    slack_creds: SlackCredentials | None,
) -> NotificationDispatcher:
    """Build a `NotificationDispatcher` from app config.

    Two paths, in priority order:

      1. `notifications: [...]` non-empty → build one channel per entry,
         honour each entry's own `levels:` filter.
      2. `notifications` empty AND legacy `slack:` block + Slack secret
         present → single-Slack back-compat: one `SlackChannel` at all
         three levels. This keeps existing deployments working unchanged.

    Both paths can yield an empty dispatcher (no channels) — that's
    legal and useful for `--dry-run` / CI / air-gapped runs. The
    dispatcher methods become no-ops.
    """
    channels: list[tuple[NotificationChannel, set[NotificationLevel]]] = []

    if config.notifications:
        for entry in config.notifications:
            channel = _build_channel(entry, slack_creds=slack_creds)
            allowed = {NotificationLevel(level) for level in entry.levels}
            channels.append((channel, allowed))
    elif slack_creds is not None:
        # Legacy single-Slack shape — operator didn't migrate to the
        # `notifications:` list but the `slack:` block + secret are
        # present. Translate to a single SlackChannel at all levels.
        channels.append(
            (
                SlackChannel(slack_creds, channel=config.slack.channel),
                set(NotificationLevel),
            )
        )

    return NotificationDispatcher(channels)


def _build_channel(
    entry: object,  # NotificationConfig union — typed at call site
    *,
    slack_creds: SlackCredentials | None,
) -> NotificationChannel:
    """Instantiate the concrete channel for one config entry."""
    # Avoid an import cycle: models imports from this package for the
    # `NotificationLevel` literal, so we resolve the discriminator here
    # via duck-typing on `kind`.
    kind = getattr(entry, "kind", None)
    if kind == "slack":
        if slack_creds is None:
            raise ConfigError(
                "notifications[].kind=slack but no SlackCredentials were loaded "
                "(check the iac-cartographer/slack secret)"
            )
        # Per-entry `channel:` override takes precedence over the legacy
        # top-level `slack.channel`. Mirror the same fallback the old
        # call site used.
        channel_override = getattr(entry, "channel", None)
        return SlackChannel(slack_creds, channel=channel_override)
    raise ConfigError(f"unknown notifications[].kind: {kind!r}")


__all__ = [
    "NotificationChannel",
    "NotificationDispatcher",
    "NotificationLevel",
    "SlackChannel",
    "build_dispatcher",
]
