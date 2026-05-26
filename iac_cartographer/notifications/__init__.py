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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from iac_cartographer.constants import ConfigError
from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel
from iac_cartographer.notifications.discord import DiscordChannel
from iac_cartographer.notifications.dispatcher import NotificationDispatcher
from iac_cartographer.notifications.email import EmailChannel
from iac_cartographer.notifications.opsgenie import OpsgenieChannel
from iac_cartographer.notifications.pagerduty import PagerDutyChannel
from iac_cartographer.notifications.slack import SlackChannel
from iac_cartographer.notifications.slack_webhook import SlackWebhookChannel
from iac_cartographer.notifications.sns import SnsChannel
from iac_cartographer.notifications.stdout import StdoutChannel
from iac_cartographer.notifications.teams import TeamsChannel
from iac_cartographer.notifications.webhook import GenericWebhookChannel

if TYPE_CHECKING:
    from iac_cartographer.models import (
        AppConfig,
        DiscordCredentials,
        EmailCredentials,
        OpsgenieCredentials,
        PagerDutyCredentials,
        SlackCredentials,
        SlackWebhookCredentials,
        TeamsCredentials,
        WebhookCredentials,
    )


@dataclass(frozen=True)
class NotificationSecrets:
    """Bundle of webhook-family credentials passed into `build_dispatcher`.

    Each field is optional — only the secrets matching kinds actually
    used in `notifications:` need to be loaded. The factory raises
    `ConfigError` if a `kind: X` entry is present but the matching
    credentials are `None`.

    `slack` covers both the legacy single-channel `slack:` block (used
    when `notifications:` is empty) and the modern `kind: slack` entries.
    """

    slack: SlackCredentials | None = None
    webhook: WebhookCredentials | None = None
    slack_webhook: SlackWebhookCredentials | None = None
    teams: TeamsCredentials | None = None
    email: EmailCredentials | None = None
    pagerduty: PagerDutyCredentials | None = None
    opsgenie: OpsgenieCredentials | None = None
    discord: DiscordCredentials | None = None
    # No `sns` or `stdout` fields:
    # - SNS uses the AWS credential chain (identity-based).
    # - Stdout has no credential — it just writes to a process stream.


def build_dispatcher(
    config: AppConfig,
    *,
    secrets: NotificationSecrets,
) -> NotificationDispatcher:
    """Build a `NotificationDispatcher` from app config + loaded secrets.

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
            channel = _build_channel(entry, secrets=secrets)
            allowed = {NotificationLevel(level) for level in entry.levels}
            channels.append((channel, allowed))
    elif secrets.slack is not None:
        # Legacy single-Slack shape — operator didn't migrate to the
        # `notifications:` list but the `slack:` block + secret are
        # present. Translate to a single SlackChannel at all levels.
        channels.append(
            (
                SlackChannel(secrets.slack, channel=config.slack.channel),
                set(NotificationLevel),
            )
        )

    return NotificationDispatcher(channels)


def _build_channel(
    entry: object,  # NotificationConfig union — typed at call site
    *,
    secrets: NotificationSecrets,
) -> NotificationChannel:
    """Instantiate the concrete channel for one config entry."""
    # Resolve the discriminator via duck-typing on `kind` to avoid an
    # import cycle (models imports from this package for the
    # `NotificationLevel` literal).
    kind = getattr(entry, "kind", None)

    if kind == "slack":
        if secrets.slack is None:
            raise ConfigError(
                "notifications[].kind=slack but no SlackCredentials were loaded "
                "(check the iac-cartographer/slack secret)"
            )
        channel_override = getattr(entry, "channel", None)
        return SlackChannel(secrets.slack, channel=channel_override)

    if kind == "webhook":
        if secrets.webhook is None:
            raise ConfigError(
                "notifications[].kind=webhook but no WebhookCredentials were loaded "
                "(check the iac-cartographer/webhook secret)"
            )
        extra_headers = getattr(entry, "extra_headers", {})
        return GenericWebhookChannel(secrets.webhook, extra_headers=extra_headers)

    if kind == "slack_webhook":
        if secrets.slack_webhook is None:
            raise ConfigError(
                "notifications[].kind=slack_webhook but no SlackWebhookCredentials "
                "were loaded (check the iac-cartographer/slack_webhook secret)"
            )
        return SlackWebhookChannel(secrets.slack_webhook)

    if kind == "teams":
        if secrets.teams is None:
            raise ConfigError(
                "notifications[].kind=teams but no TeamsCredentials were loaded "
                "(check the iac-cartographer/teams secret)"
            )
        return TeamsChannel(secrets.teams)

    if kind == "email":
        if secrets.email is None:
            raise ConfigError(
                "notifications[].kind=email but no EmailCredentials were loaded "
                "(check the iac-cartographer/email secret)"
            )
        return EmailChannel(
            secrets.email,
            smtp_host=entry.smtp_host,
            smtp_port=getattr(entry, "smtp_port", 587),
            from_address=entry.from_address,
            to_addresses=entry.to_addresses,
            use_tls=getattr(entry, "use_tls", True),
            subject_prefix=getattr(entry, "subject_prefix", "[iac-cartographer]"),
        )

    if kind == "sns":
        # Identity-based — no `secrets.sns` field to check.
        return SnsChannel(
            topic_arn=entry.topic_arn,
            region=getattr(entry, "region", None),
        )

    if kind == "pagerduty":
        if secrets.pagerduty is None:
            raise ConfigError(
                "notifications[].kind=pagerduty but no PagerDutyCredentials were loaded "
                "(check the iac-cartographer/pagerduty secret)"
            )
        return PagerDutyChannel(secrets.pagerduty)

    if kind == "opsgenie":
        if secrets.opsgenie is None:
            raise ConfigError(
                "notifications[].kind=opsgenie but no OpsgenieCredentials were loaded "
                "(check the iac-cartographer/opsgenie secret)"
            )
        return OpsgenieChannel(secrets.opsgenie, region=getattr(entry, "region", "us"))

    if kind == "discord":
        if secrets.discord is None:
            raise ConfigError(
                "notifications[].kind=discord but no DiscordCredentials were loaded "
                "(check the iac-cartographer/discord secret)"
            )
        return DiscordChannel(
            secrets.discord,
            username=getattr(entry, "username", None),
            avatar_url=getattr(entry, "avatar_url", None),
        )

    if kind == "stdout":
        # No credentials, no I/O setup — just resolve the stream.
        return StdoutChannel(stream=getattr(entry, "stream", "stdout"))

    raise ConfigError(f"unknown notifications[].kind: {kind!r}")


__all__ = [
    "DiscordChannel",
    "EmailChannel",
    "GenericWebhookChannel",
    "NotificationChannel",
    "NotificationDispatcher",
    "NotificationLevel",
    "NotificationSecrets",
    "OpsgenieChannel",
    "PagerDutyChannel",
    "SlackChannel",
    "SlackWebhookChannel",
    "SnsChannel",
    "StdoutChannel",
    "TeamsChannel",
    "build_dispatcher",
]
