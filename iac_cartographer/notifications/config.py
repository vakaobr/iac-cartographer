"""Notification subsystem config + credential models.

The per-channel notification config models, the `NotificationConfig`
discriminated union, the legacy top-level `SlackConfig`, and the
notification credential models live here beside the channel
implementations that consume them.

Re-exported from `iac_cartographer.models` for back-compat.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from iac_cartographer.models import _Strict


class SlackConfig(_Strict):
    # `#channel-name` or a Slack channel ID (`C0...`). The bot token must be
    # invited to this channel.
    channel: str = "#alerts"


# Per-channel notification config. The discriminated union below grows as
# new channels ship (Teams, RocketChat, email, SNS, generic webhook, …);
# Slack is the first concrete entry because it's the one that already
# exists. Each entry carries its own `levels:` filter so operators can
# route info → chat, errors → pager/email/etc. independently.
class _BaseNotificationConfig(_Strict):
    """Common shape every concrete `NotificationConfig.kind` shares."""

    # `kind` is the discriminator — Pydantic uses it to pick which
    # subclass to instantiate when validating the `notifications:` list.
    # Set by each subclass with a `Literal` default.
    #: Per-entry severity filter. Default is "fire on everything";
    #: narrow to e.g. `[error]` for a PagerDuty-style escalation channel.
    levels: list[Literal["info", "warn", "error"]] = Field(default_factory=lambda: ["info", "warn", "error"])


class SlackNotificationConfig(_BaseNotificationConfig):
    """Slack workspace channel (bot-token `chat.postMessage`).

    Credentials come from the `iac-cartographer/slack` secret (same
    secret name as the legacy `slack:` block; both shapes can coexist
    during a migration). `channel` overrides the top-level `slack.channel`
    when set — useful when one Slack workspace publishes notifications to
    multiple channels (e.g. `#infra-info` for info, `#infra-alerts` for
    error).
    """

    kind: Literal["slack"] = "slack"
    # `#channel-name` or channel ID (`C0...`). When unset, falls back to
    # the top-level `slack.channel` so single-Slack deployments need only
    # one place to configure the destination.
    channel: str | None = None


class WebhookNotificationConfig(_BaseNotificationConfig):
    """Generic JSON-shaped webhook channel.

    Posts our own stable schema (`{schema, level, message, ts, source}`)
    to an arbitrary URL. Catch-all destination for anything that doesn't
    fit a dedicated channel: internal observability platforms, custom
    Lambda/Cloud Function forwarders, generic-event intake URLs.

    Credentials come from the `iac-cartographer/webhook` secret as
    `{"url": "..."}`. `extra_headers` (in this config block, not the
    secret) accepts arbitrary HTTP headers — useful when the endpoint
    wants a bearer token on top of the URL-embedded secret.
    """

    kind: Literal["webhook"] = "webhook"
    extra_headers: dict[str, str] = Field(default_factory=dict)


class SlackWebhookNotificationConfig(_BaseNotificationConfig):
    """Slack-compatible incoming-webhook channel.

    Posts the Slack-shaped `{"text": "..."}` payload format that's the
    de-facto interop standard for chat platforms. One channel covers
    three destinations:

      * Slack incoming webhooks (URL-based; alternative to the bot-token
        `chat.postMessage` API used by `kind: slack`).
      * RocketChat (native Slack-compat at any webhook URL).
      * Mattermost (same; self-hosted regulated / on-prem deployments).

    Credentials come from the `iac-cartographer/slack_webhook` secret as
    `{"url": "..."}`. The URL itself IS the credential — never check it
    into version-controlled config.
    """

    kind: Literal["slack_webhook"] = "slack_webhook"


class TeamsNotificationConfig(_BaseNotificationConfig):
    """Microsoft Teams channel via workflow webhook (Adaptive Card).

    Posts an Adaptive Card v1.4 inside the Teams `attachments` envelope.
    Works with both the modern Workflow webhooks (Power Automate) and
    the legacy Office 365 Connector webhooks — same payload shape.

    Severity maps to Adaptive Card colours (info=good, warn=warning,
    error=attention) so messages are visually distinguishable in the
    Teams channel.

    Credentials come from the `iac-cartographer/teams` secret as
    `{"url": "..."}`. The workflow URL embeds a SAS token, so never
    check it into version-controlled config.
    """

    kind: Literal["teams"] = "teams"


class EmailNotificationConfig(_BaseNotificationConfig):
    """SMTP-backed email channel.

    Sends multipart/alternative messages with an HTML body (severity
    rendered as a coloured header) and a plain-text fallback. Tuned
    for the operator-inbox shape: scannable subject + full message
    in the body.

    Credentials come from the `iac-cartographer/email` secret as
    `{"username": "...", "password": "..."}`. Most managed providers
    fit this shape (Postmark, SendGrid, Mailgun, AWS SES SMTP
    credentials, internal Postfix relays).

    Requires `pip install 'iac-cartographer[email]'` — the
    `aiosmtplib` SDK is lazy-imported on first send and the channel
    logs + skips if the dep is missing (so a misconfigured channel
    doesn't sink the run).
    """

    kind: Literal["email"] = "email"
    # SMTP server hostname (e.g. `smtp.sendgrid.net`).
    smtp_host: str
    # Submission port. 587 = STARTTLS (default); 465 = legacy implicit
    # TLS is NOT supported — open an issue if you need it.
    smtp_port: int = 587
    # Envelope sender. Authenticate as `creds.username` but send from
    # this address (often a no-reply alias on the same domain).
    from_address: str
    # Recipient list. Multiple addresses fan out via the SMTP server's
    # `RCPT TO` — typically a small list (oncall@, devops@). For
    # large fan-out, use SNS or a distribution list on your mail
    # server.
    to_addresses: list[str] = Field(min_length=1)
    # Set to `false` only for in-cluster relays that are already on an
    # authenticated network. Never on the public internet.
    use_tls: bool = True
    # Replaces `[iac-cartographer]` in the subject line. Useful when
    # multiple iac-cartographer deployments share an inbox (per-region
    # or per-tenant prefixes — `[iac-cart-eu]`, `[iac-cart-prod]`).
    subject_prefix: str = "[iac-cartographer]"


class SnsNotificationConfig(_BaseNotificationConfig):
    """AWS SNS topic publish channel.

    Identity-based (no `iac-cartographer/sns` secret) — uses the
    standard AWS credential chain. The IAM principal running
    iac-cartographer needs `sns:Publish` on the topic ARN.

    SNS handles downstream fanout (email, SMS, Lambda, SQS, HTTPS,
    mobile push) so you can subscribe many endpoints to one topic
    from one place. Each message carries a `level` MessageAttribute
    so SNS filter policies can route info → Lambda, error → email.
    """

    kind: Literal["sns"] = "sns"
    # ARN of the SNS topic to publish to.
    topic_arn: str
    # AWS region. When unset, boto3's default resolution applies
    # (env var, profile, instance metadata).
    region: str | None = None


class PagerDutyNotificationConfig(_BaseNotificationConfig):
    """PagerDuty incident-trigger channel (Events API v2).

    Triggers incidents via the public events intake endpoint
    (`events.pagerduty.com/v2/enqueue`). Authenticated by a
    **routing key** (per-service integration key) rather than a user
    token, so one channel = one PagerDuty Service.

    Strongly recommend narrowing this to `levels: [error]` to avoid
    paging on info/warn — the channel itself doesn't enforce that,
    leaving the policy choice to the operator.

    Credentials come from the `iac-cartographer/pagerduty` secret as
    `{"routing_key": "..."}`. Get the routing key from PagerDuty's
    Service → Integrations → Events API v2 → Integration Key.
    """

    kind: Literal["pagerduty"] = "pagerduty"


class OpsgenieNotificationConfig(_BaseNotificationConfig):
    """Opsgenie alert-creation channel (Alerts API).

    Creates alerts via the public Alerts API. Authenticated by a
    team / integration API key in an `Authorization: GenieKey <key>`
    header.

    Same routing-policy advice as PagerDuty: narrow to
    `levels: [error]` for page-on-error behaviour.

    Credentials come from the `iac-cartographer/opsgenie` secret as
    `{"api_key": "..."}`.

    Region split — Opsgenie maintains two independent control planes
    (US + EU); EU customers MUST set `region: "eu"` so the channel
    targets `api.eu.opsgenie.com`. A US key will be rejected by the
    EU host and vice-versa.
    """

    kind: Literal["opsgenie"] = "opsgenie"
    # Opsgenie control-plane region. `"us"` = api.opsgenie.com (default),
    # `"eu"` = api.eu.opsgenie.com. Match the region your API key was
    # issued on — the two planes are NOT linked.
    region: Literal["us", "eu"] = "us"


class DiscordNotificationConfig(_BaseNotificationConfig):
    """Discord webhook channel.

    Posts `{"content": "<emoji> <message>"}` to a per-channel Discord
    Incoming Webhook URL. Designed for community / homelab
    deployments where Slack would be overkill.

    Credentials come from the `iac-cartographer/discord` secret as
    `{"url": "https://discord.com/api/webhooks/..."}`. The URL
    embeds the webhook ID + token, so the URL IS the credential.

    `username` and `avatar_url` are optional per-message overrides;
    when set they replace the defaults baked into the webhook by
    whoever created it in the Discord UI. Useful when one Discord
    server hosts notifications from multiple deployments (per-env or
    per-tenant identity).
    """

    kind: Literal["discord"] = "discord"
    # Override the webhook's default username for messages from this
    # channel. Defaults to None (= use the webhook's own setting).
    username: str | None = None
    # Override the webhook's default avatar image URL. Defaults to
    # None (= use the webhook's own setting).
    avatar_url: str | None = None


class StdoutNotificationConfig(_BaseNotificationConfig):
    """Stdout / stderr notification channel.

    Emits one line per notification to the configured stream — in
    either JSON Lines (default; same payload schema as the generic
    webhook channel) or human-readable text. Useful for CI runs,
    air-gapped deployments, and local dev where chat / pager / SMTP
    destinations aren't available but a log aggregator picks up stdout.

    No credentials, no HTTP, no SDK — `print()` is the only I/O.
    """

    kind: Literal["stdout"] = "stdout"
    # `"stdout"` (default) or `"stderr"`. Use `stderr` when stdout
    # is reserved for machine-parseable pipeline output.
    stream: Literal["stdout", "stderr"] = "stdout"
    # `"jsonl"` (default; one structured JSON line per event, machine-
    # parseable, same schema as the webhook channel) or `"text"` (one
    # human-readable line per event, shaped `[iac-cartographer][LEVEL]
    # message`). Default keeps backwards compatibility for existing
    # log-aggregator pipelines.
    format: Literal["jsonl", "text"] = "jsonl"


# Discriminated union — extend as new channels ship.
NotificationConfig = (
    SlackNotificationConfig
    | WebhookNotificationConfig
    | SlackWebhookNotificationConfig
    | TeamsNotificationConfig
    | EmailNotificationConfig
    | SnsNotificationConfig
    | PagerDutyNotificationConfig
    | OpsgenieNotificationConfig
    | DiscordNotificationConfig
    | StdoutNotificationConfig
)


# ─── Notification credentials (one model per Secrets Manager entry) ────────


class SlackCredentials(_Strict):
    bot_token: str
    # Fallback channel, used by `SlackChannel` when no explicit `channel` is
    # passed at construction — i.e. a `notifications[].kind: slack` entry that
    # omits `channel`, or the legacy `slack:` block without a channel set.
    # The legacy single-Slack path always passes `config.slack.channel`, so
    # this only bites the modern-list-without-channel case; keep it as the
    # safety net for that path.
    channel_id: str | None = None


# All three webhook-family creds share the same shape (just a URL) but stay
# separate so the logical secret name is encoded in the type — same pattern
# the LLM credential classes follow (Anthropic / OpenAI / AzureOpenAI all
# have a single `api_key: str` but live as distinct types).


class WebhookCredentials(_Strict):
    """Generic webhook URL — `iac-cartographer/webhook` secret."""

    url: str


class SlackWebhookCredentials(_Strict):
    """Slack-compatible incoming webhook URL — `iac-cartographer/slack_webhook` secret.

    Works for native Slack incoming webhooks, RocketChat, and Mattermost
    (same payload shape across all three).
    """

    url: str


class TeamsCredentials(_Strict):
    """Microsoft Teams workflow-webhook URL — `iac-cartographer/teams` secret.

    The URL embeds a SAS token, so the entire URL is the credential.
    """

    url: str


class EmailCredentials(_Strict):
    """SMTP username + password — `iac-cartographer/email` secret.

    Most managed SMTP providers fit this shape. Some quirks:

      * **AWS SES** — the SMTP credentials are NOT your IAM
        access-key pair; generate dedicated SMTP credentials via the
        SES console.
      * **SendGrid** — `username` is the literal string `"apikey"`,
        `password` is the SendGrid API key.
      * **Postmark** — `username` is your server token, `password` is
        the same token.
    """

    username: str
    password: str


class PagerDutyCredentials(_Strict):
    """PagerDuty routing key — `iac-cartographer/pagerduty` secret.

    Get the value from PagerDuty's Service → Integrations →
    Events API v2 → Integration Key. It's a ~32-char token bound to
    one Service / escalation policy.
    """

    routing_key: str


class OpsgenieCredentials(_Strict):
    """Opsgenie API key — `iac-cartographer/opsgenie` secret.

    Issued from a team integration or API integration in the
    Opsgenie console. Region-bound — a US-plane key will NOT work
    against the EU plane and vice-versa; set `region:` on the
    channel config to match.
    """

    api_key: str


class DiscordCredentials(_Strict):
    """Discord webhook URL — `iac-cartographer/discord` secret.

    The URL embeds the webhook ID + token, so the entire URL is the
    credential. Create one via Discord channel settings →
    Integrations → Webhooks → New Webhook.
    """

    url: str
