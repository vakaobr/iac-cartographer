"""Tests for the multi-channel notification dispatcher + factory.

Covers:
  * Dispatcher fanout — every eligible channel receives the message.
  * Level filter — channels with `levels=["error"]` skip info/warn.
  * Per-channel failure isolation — one raising channel doesn't take
    down the others.
  * Empty dispatcher — no-op, no error.
  * `build_dispatcher` back-compat shim — empty `notifications:` list +
    Slack creds → single SlackChannel at all three levels.
  * `build_dispatcher` modern path — `notifications: [...]` honoured.
  * `build_dispatcher` rejects `slack` kind when no creds were loaded.
"""

from __future__ import annotations

import pytest

from iac_cartographer.constants import ConfigError
from iac_cartographer.models import (
    AppConfig,
    SlackCredentials,
    SlackNotificationConfig,
)
from iac_cartographer.notifications import (
    NotificationChannel,
    NotificationDispatcher,
    NotificationLevel,
    NotificationSecrets,
    SlackChannel,
    build_dispatcher,
)


class _RecordingChannel(NotificationChannel):
    """Test double — records every notify call and exposes close-counter."""

    def __init__(self, name: str = "recording") -> None:
        self.name = name
        self.calls: list[tuple[NotificationLevel, str]] = []
        self.close_count = 0

    async def notify(self, level: NotificationLevel, message: str) -> None:
        self.calls.append((level, message))

    async def close(self) -> None:
        self.close_count += 1


class _RaisingChannel(NotificationChannel):
    """Test double — notify always raises. Lets us verify isolation."""

    name = "raising"

    async def notify(self, level: NotificationLevel, message: str) -> None:
        raise RuntimeError("upstream is down")


# ── Dispatcher behaviour ──────────────────────────────────────────────


async def test_dispatcher_fans_out_to_every_eligible_channel() -> None:
    ch_a = _RecordingChannel("a")
    ch_b = _RecordingChannel("b")
    d = NotificationDispatcher(
        [
            (ch_a, set(NotificationLevel)),
            (ch_b, set(NotificationLevel)),
        ]
    )

    await d.info("hello")

    assert ch_a.calls == [(NotificationLevel.INFO, "hello")]
    assert ch_b.calls == [(NotificationLevel.INFO, "hello")]


async def test_dispatcher_filters_by_level_per_channel() -> None:
    chat = _RecordingChannel("chat")
    pager = _RecordingChannel("pager")
    d = NotificationDispatcher(
        [
            (chat, set(NotificationLevel)),
            (pager, {NotificationLevel.ERROR}),  # errors only
        ]
    )

    await d.info("starting")
    await d.warn("careful")
    await d.error("kaboom")

    assert chat.calls == [
        (NotificationLevel.INFO, "starting"),
        (NotificationLevel.WARN, "careful"),
        (NotificationLevel.ERROR, "kaboom"),
    ]
    assert pager.calls == [(NotificationLevel.ERROR, "kaboom")]


async def test_dispatcher_isolates_per_channel_failures() -> None:
    """A raising channel doesn't sink the dispatcher — the second
    channel still gets its message."""
    ok = _RecordingChannel("ok")
    d = NotificationDispatcher(
        [
            (_RaisingChannel(), set(NotificationLevel)),
            (ok, set(NotificationLevel)),
        ]
    )

    await d.info("hi")  # must not raise

    assert ok.calls == [(NotificationLevel.INFO, "hi")]


async def test_dispatcher_with_zero_channels_is_a_noop() -> None:
    """`notifications: []` and no Slack fallback → silent dispatcher."""
    d = NotificationDispatcher([])
    await d.info("nobody home")
    await d.warn("still nobody home")
    await d.error("nobody")
    await d.close()  # must not raise


async def test_dispatcher_close_invokes_every_channel() -> None:
    a = _RecordingChannel("a")
    b = _RecordingChannel("b")
    d = NotificationDispatcher(
        [
            (a, set(NotificationLevel)),
            (b, set(NotificationLevel)),
        ]
    )

    await d.close()

    assert a.close_count == 1
    assert b.close_count == 1


# ── build_dispatcher factory ──────────────────────────────────────────


def _slack_creds() -> SlackCredentials:
    return SlackCredentials(bot_token="xoxb", channel_id="C-default")


def test_build_dispatcher_legacy_path_single_slack() -> None:
    """Empty `notifications:` + Slack creds → one SlackChannel at all levels."""
    config = AppConfig()  # notifications=[] by default
    d = build_dispatcher(config, secrets=NotificationSecrets(slack=_slack_creds()))

    assert len(d._channels) == 1
    channel, levels = d._channels[0]
    assert isinstance(channel, SlackChannel)
    assert levels == set(NotificationLevel)


def test_build_dispatcher_legacy_path_uses_top_level_slack_channel() -> None:
    """Legacy shape pulls `slack.channel` from the top-level block."""
    config = AppConfig.model_validate({"slack": {"channel": "#infra-alerts"}})
    d = build_dispatcher(config, secrets=NotificationSecrets(slack=_slack_creds()))

    channel, _ = d._channels[0]
    assert isinstance(channel, SlackChannel)
    assert channel._channel == "#infra-alerts"


def test_build_dispatcher_modern_path_with_per_entry_levels() -> None:
    """`notifications: [...]` honours each entry's own levels filter."""
    config = AppConfig.model_validate(
        {
            "notifications": [
                {"kind": "slack", "channel": "#chat"},
                {"kind": "slack", "channel": "#alerts", "levels": ["error"]},
            ]
        }
    )
    d = build_dispatcher(config, secrets=NotificationSecrets(slack=_slack_creds()))

    assert len(d._channels) == 2
    chat_ch, chat_levels = d._channels[0]
    alerts_ch, alerts_levels = d._channels[1]

    assert isinstance(chat_ch, SlackChannel)
    assert chat_ch._channel == "#chat"
    assert chat_levels == set(NotificationLevel)

    assert isinstance(alerts_ch, SlackChannel)
    assert alerts_ch._channel == "#alerts"
    assert alerts_levels == {NotificationLevel.ERROR}


def test_build_dispatcher_modern_path_ignores_top_level_slack_block() -> None:
    """When `notifications:` is non-empty, the legacy `slack:` block is
    NOT auto-translated — operators have opted into explicit routing."""
    config = AppConfig.model_validate(
        {
            "slack": {"channel": "#legacy-channel"},
            "notifications": [{"kind": "slack", "channel": "#explicit"}],
        }
    )
    d = build_dispatcher(config, secrets=NotificationSecrets(slack=_slack_creds()))

    assert len(d._channels) == 1  # the legacy block is dropped
    channel, _ = d._channels[0]
    assert isinstance(channel, SlackChannel)
    assert channel._channel == "#explicit"


def test_build_dispatcher_empty_config_and_no_slack_creds_is_silent() -> None:
    """`--dry-run` / CI / air-gapped: no slack secret loaded, no
    `notifications:` configured → empty dispatcher, no error."""
    config = AppConfig()
    d = build_dispatcher(config, secrets=NotificationSecrets())
    assert d._channels == []


def test_build_dispatcher_rejects_slack_kind_without_creds() -> None:
    """Explicit `notifications: [slack]` but no Slack secret loaded is a
    misconfiguration — fail loud at startup, not at first error."""
    config = AppConfig.model_validate({"notifications": [{"kind": "slack"}]})

    with pytest.raises(ConfigError, match=r"notifications.*slack"):
        build_dispatcher(config, secrets=NotificationSecrets())


def test_build_dispatcher_wires_webhook_channel() -> None:
    """kind: webhook → GenericWebhookChannel with the loaded URL + headers."""
    from iac_cartographer.models import WebhookCredentials
    from iac_cartographer.notifications import GenericWebhookChannel

    config = AppConfig.model_validate(
        {
            "notifications": [
                {"kind": "webhook", "extra_headers": {"Authorization": "Bearer t"}},
            ]
        }
    )
    secrets = NotificationSecrets(webhook=WebhookCredentials(url="https://hook.example.com"))
    d = build_dispatcher(config, secrets=secrets)

    channel, _ = d._channels[0]
    assert isinstance(channel, GenericWebhookChannel)
    assert channel._url == "https://hook.example.com"
    assert channel._extra_headers == {"Authorization": "Bearer t"}


def test_build_dispatcher_wires_slack_webhook_channel() -> None:
    """kind: slack_webhook → SlackWebhookChannel with the loaded URL."""
    from iac_cartographer.models import SlackWebhookCredentials
    from iac_cartographer.notifications import SlackWebhookChannel

    config = AppConfig.model_validate({"notifications": [{"kind": "slack_webhook"}]})
    secrets = NotificationSecrets(slack_webhook=SlackWebhookCredentials(url="https://hooks.slack.com/services/x/y/z"))
    d = build_dispatcher(config, secrets=secrets)

    channel, _ = d._channels[0]
    assert isinstance(channel, SlackWebhookChannel)
    assert channel._url == "https://hooks.slack.com/services/x/y/z"


def test_build_dispatcher_wires_teams_channel() -> None:
    """kind: teams → TeamsChannel with the loaded URL."""
    from iac_cartographer.models import TeamsCredentials
    from iac_cartographer.notifications import TeamsChannel

    config = AppConfig.model_validate({"notifications": [{"kind": "teams"}]})
    secrets = NotificationSecrets(teams=TeamsCredentials(url="https://teams.example.com"))
    d = build_dispatcher(config, secrets=secrets)

    channel, _ = d._channels[0]
    assert isinstance(channel, TeamsChannel)
    assert channel._url == "https://teams.example.com"


def test_build_dispatcher_rejects_webhook_kind_without_creds() -> None:
    config = AppConfig.model_validate({"notifications": [{"kind": "webhook"}]})
    with pytest.raises(ConfigError, match=r"notifications.*webhook"):
        build_dispatcher(config, secrets=NotificationSecrets())


def test_build_dispatcher_rejects_slack_webhook_kind_without_creds() -> None:
    config = AppConfig.model_validate({"notifications": [{"kind": "slack_webhook"}]})
    with pytest.raises(ConfigError, match=r"notifications.*slack_webhook"):
        build_dispatcher(config, secrets=NotificationSecrets())


def test_build_dispatcher_rejects_teams_kind_without_creds() -> None:
    config = AppConfig.model_validate({"notifications": [{"kind": "teams"}]})
    with pytest.raises(ConfigError, match=r"notifications.*teams"):
        build_dispatcher(config, secrets=NotificationSecrets())


def test_build_dispatcher_wires_email_channel() -> None:
    """kind: email → EmailChannel with SMTP config + loaded credentials."""
    from iac_cartographer.models import EmailCredentials
    from iac_cartographer.notifications import EmailChannel

    config = AppConfig.model_validate(
        {
            "notifications": [
                {
                    "kind": "email",
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 587,
                    "from_address": "noreply@example.com",
                    "to_addresses": ["ops@example.com"],
                }
            ]
        }
    )
    secrets = NotificationSecrets(email=EmailCredentials(username="u", password="p"))
    d = build_dispatcher(config, secrets=secrets)

    channel, _ = d._channels[0]
    assert isinstance(channel, EmailChannel)
    assert channel._smtp_host == "smtp.example.com"
    assert channel._to == ["ops@example.com"]
    assert channel._username == "u"


def test_build_dispatcher_wires_sns_channel_without_secret() -> None:
    """kind: sns → SnsChannel using only config (no secret to load —
    auth via the AWS credential chain)."""
    from iac_cartographer.notifications import SnsChannel

    config = AppConfig.model_validate(
        {
            "notifications": [
                {
                    "kind": "sns",
                    "topic_arn": "arn:aws:sns:eu-central-1:000:t",
                    "region": "eu-central-1",
                }
            ]
        }
    )
    d = build_dispatcher(config, secrets=NotificationSecrets())

    channel, _ = d._channels[0]
    assert isinstance(channel, SnsChannel)
    assert channel._topic_arn == "arn:aws:sns:eu-central-1:000:t"
    assert channel._region == "eu-central-1"


def test_build_dispatcher_rejects_email_kind_without_creds() -> None:
    config = AppConfig.model_validate(
        {
            "notifications": [
                {
                    "kind": "email",
                    "smtp_host": "smtp.example.com",
                    "from_address": "noreply@example.com",
                    "to_addresses": ["ops@example.com"],
                }
            ]
        }
    )
    with pytest.raises(ConfigError, match=r"notifications.*email"):
        build_dispatcher(config, secrets=NotificationSecrets())


def test_email_config_requires_at_least_one_recipient() -> None:
    """to_addresses: [] is a misconfiguration — Pydantic rejects at validation."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "notifications": [
                    {
                        "kind": "email",
                        "smtp_host": "smtp.example.com",
                        "from_address": "noreply@example.com",
                        "to_addresses": [],
                    }
                ]
            }
        )


def test_build_dispatcher_wires_pagerduty_channel() -> None:
    """kind: pagerduty → PagerDutyChannel with the loaded routing key."""
    from iac_cartographer.models import PagerDutyCredentials
    from iac_cartographer.notifications import PagerDutyChannel

    config = AppConfig.model_validate({"notifications": [{"kind": "pagerduty", "levels": ["error"]}]})
    secrets = NotificationSecrets(pagerduty=PagerDutyCredentials(routing_key="k"))
    d = build_dispatcher(config, secrets=secrets)

    channel, levels = d._channels[0]
    assert isinstance(channel, PagerDutyChannel)
    assert channel._routing_key == "k"
    assert levels == {NotificationLevel.ERROR}


def test_build_dispatcher_wires_opsgenie_channel_with_region() -> None:
    """kind: opsgenie → OpsgenieChannel; `region: eu` threads through."""
    from iac_cartographer.models import OpsgenieCredentials
    from iac_cartographer.notifications import OpsgenieChannel

    config = AppConfig.model_validate(
        {
            "notifications": [
                {"kind": "opsgenie", "region": "eu", "levels": ["error"]},
            ]
        }
    )
    secrets = NotificationSecrets(opsgenie=OpsgenieCredentials(api_key="og-k"))
    d = build_dispatcher(config, secrets=secrets)

    channel, _ = d._channels[0]
    assert isinstance(channel, OpsgenieChannel)
    # EU plane routes to api.eu.opsgenie.com.
    assert channel._host == "https://api.eu.opsgenie.com"


def test_build_dispatcher_rejects_pagerduty_kind_without_creds() -> None:
    config = AppConfig.model_validate({"notifications": [{"kind": "pagerduty"}]})
    with pytest.raises(ConfigError, match=r"notifications.*pagerduty"):
        build_dispatcher(config, secrets=NotificationSecrets())


def test_build_dispatcher_rejects_opsgenie_kind_without_creds() -> None:
    config = AppConfig.model_validate({"notifications": [{"kind": "opsgenie"}]})
    with pytest.raises(ConfigError, match=r"notifications.*opsgenie"):
        build_dispatcher(config, secrets=NotificationSecrets())


def test_opsgenie_config_rejects_unknown_region() -> None:
    """Only `us` and `eu` are valid; anything else is a typo."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"notifications": [{"kind": "opsgenie", "region": "ap"}]})


def test_build_dispatcher_mixed_kinds() -> None:
    """All four kinds in one list — each instantiates its own channel
    type and the dispatcher fans events across them in order."""
    from iac_cartographer.models import (
        SlackWebhookCredentials,
        TeamsCredentials,
        WebhookCredentials,
    )
    from iac_cartographer.notifications import (
        GenericWebhookChannel,
        SlackChannel,
        SlackWebhookChannel,
        TeamsChannel,
    )

    config = AppConfig.model_validate(
        {
            "notifications": [
                {"kind": "slack", "channel": "#chat"},
                {"kind": "webhook"},
                {"kind": "slack_webhook"},
                {"kind": "teams", "levels": ["error"]},
            ]
        }
    )
    secrets = NotificationSecrets(
        slack=_slack_creds(),
        webhook=WebhookCredentials(url="https://hook.example.com"),
        slack_webhook=SlackWebhookCredentials(url="https://hooks.slack.com/services/x/y/z"),
        teams=TeamsCredentials(url="https://teams.example.com"),
    )
    d = build_dispatcher(config, secrets=secrets)

    assert [type(c) for c, _ in d._channels] == [
        SlackChannel,
        GenericWebhookChannel,
        SlackWebhookChannel,
        TeamsChannel,
    ]
    # Last entry's level filter is applied per-channel, not globally.
    _, teams_levels = d._channels[3]
    assert teams_levels == {NotificationLevel.ERROR}


# ── Pydantic model behaviour ─────────────────────────────────────────


def test_notification_config_defaults_to_all_levels() -> None:
    """A bare `{"kind": "slack"}` entry implies fan-out at all three levels."""
    entry = SlackNotificationConfig.model_validate({"kind": "slack"})
    assert entry.levels == ["info", "warn", "error"]
    assert entry.channel is None


def test_notification_config_rejects_unknown_level() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SlackNotificationConfig.model_validate({"kind": "slack", "levels": ["panic"]})
