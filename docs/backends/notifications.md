# Notifications

iac-cartographer posts a handful of pipeline events to operator-facing
channels — run-start, per-repo prompt-injection warnings, end-of-run
summary, fatal errors. All three severities (`info` / `warn` /
`error`) fan out through a multi-channel **dispatcher**, with a
per-channel level filter so you can route info-traffic to a chat
channel while sending errors to pager/email.

## Two configuration shapes

There are two ways to wire notifications, and they coexist:

### 1. Legacy `slack:` block (single-channel back-compat)

Existing deployments that predate the multi-channel rewrite continue to
work unchanged. The top-level `slack:` block + `iac-cartographer/slack`
secret act as if they were a single entry at all three levels:

```yaml
slack:
  channel: "#alerts"
```

When `notifications:` is unset / empty, this is what runs.

### 2. Modern `notifications:` list (multi-channel)

The recommended shape for any deployment that needs more than one
destination, or wants per-level routing. `notifications:` is a list of
channel entries; each entry carries its own `kind`, channel-specific
config, and a `levels:` filter:

```yaml
notifications:
  # Chat — fire on everything.
  - kind: slack
    channel: "#infra-info"
    # levels defaults to [info, warn, error] when omitted.

  # Pager-style escalation — only errors, separate channel.
  - kind: slack
    channel: "#infra-oncall"
    levels: [error]
```

When `notifications:` is non-empty, the legacy `slack:` block is
**ignored**. Operators who opt in to the explicit list own the full
routing — no surprise extra channels.

## How fanout works

- **Concurrent.** Each notification is sent to every eligible channel
  via `asyncio.gather(..., return_exceptions=True)` — a slow webhook
  doesn't block a fast one.
- **Filtered.** A channel's `levels:` filter is applied before
  `notify()` runs, so a channel only sees the severities it's
  configured for.
- **Failure-isolated.** A raising channel is logged with the channel
  name (`notifier slack: error post raised`) and the run continues.
  One broken destination does NOT sink the pipeline.

## Channels

Four channels ship today. The follow-up roadmap covers email, SNS,
PagerDuty / Opsgenie, Discord, and stdout/JSONL.

| `kind` | Status | Notes |
|---|---|---|
| `slack` | Shipped | Bot-token `chat.postMessage` to a channel. Same shape as the legacy block. |
| `webhook` | Shipped | Generic JSON POST with our own stable schema (`{schema, level, message, ts, source}`). Catch-all for custom endpoints. |
| `slack_webhook` | Shipped | Slack-compatible incoming webhook. Drop-in for native Slack incoming webhooks, RocketChat, and Mattermost. |
| `teams` | Shipped | Microsoft Teams workflow webhook + Adaptive Card v1.4. Severity → colour mapping (good / warning / attention). |
| `email` | Shipped | SMTP via `aiosmtplib`. Multipart/alternative with HTML severity-coloured header + plain-text fallback. |
| `sns` | Shipped | AWS SNS topic publish — identity-based (no stored secret). SNS fans downstream to email / SMS / Lambda / SQS / HTTPS / mobile push. |
| `pagerduty` | Shipped | PagerDuty Events API v2 — triggers incidents via per-Service routing key. Pair with `levels: [error]` for page-on-error. |
| `opsgenie` | Shipped | Opsgenie Alerts API — `GenieKey` auth, US + EU region split. Level → priority mapping (info=P5, warn=P3, error=P1). |
| `discord` | Coming next | Community / homelab. |
| `stdout` | Coming next | JSON Lines on stdout — air-gapped + CI friendly. |

Adding a new channel is small surface area: subclass
`NotificationChannel` (one async `notify(level, message)` method),
register a config kind in `iac_cartographer.models`, and add a branch
to `iac_cartographer.notifications.build_dispatcher`.

## Generic webhook

The catch-all channel — POST a JSON document with our own stable schema
to any URL. Useful for internal observability platforms, custom Lambda
/ Cloud Function forwarders, or any endpoint that doesn't fit a
dedicated channel.

```yaml
notifications:
  - kind: webhook
    extra_headers:                          # optional
      Authorization: "Bearer my-token"
    levels: [warn, error]                   # optional
```

Credentials live in the `iac-cartographer/webhook` secret as
`{"url": "https://your-endpoint.example.com/notify"}`. The URL itself
is the credential — most webhook providers embed a per-tenant secret
in the URL, so never check it into version-controlled config.

**Payload schema (stable):**

```json
{
  "schema": "iac-cartographer.notification.v1",
  "level": "info",
  "message": "iac-cartographer: run starting",
  "ts": "2026-05-26T10:30:00Z",
  "source": "iac-cartographer"
}
```

The `schema` field is a change-detect anchor — a bump to `v2` would
mean the payload shape changed and downstream consumers need updating.

## Slack-compatible incoming webhook (`slack_webhook`)

Drop-in destination for three platforms that all accept the
Slack-shaped `{"text": "..."}` payload format:

- **Slack incoming webhooks** — the URL-based posting path Slack
  supports alongside the bot-token API. Use this `kind` when you
  don't want to run a bot user (no token rotation, no channel
  invites).
- **RocketChat** — accepts Slack-shaped payloads natively at any
  incoming-webhook URL.
- **Mattermost** — same. Common choice for self-hosted /
  regulated / on-prem deployments.

```yaml
notifications:
  - kind: slack_webhook
```

Credentials live in the `iac-cartographer/slack_webhook` secret as
`{"url": "https://hooks.slack.com/services/T000/B000/XYZ"}` (or the
equivalent RocketChat / Mattermost webhook URL).

Same `:white_check_mark:` / `:warning:` / `:x:` emoji prefixes as the
bot-token Slack channel — chats look identical regardless of which
Slack transport is in play.

## Microsoft Teams (`teams`)

Posts an Adaptive Card v1.4 to a Teams workflow webhook URL. Works
with both the modern **Workflow webhooks** (Power Automate — Microsoft
is migrating everyone here) and the legacy **Office 365 Connector**
webhooks. Same payload envelope for either.

```yaml
notifications:
  - kind: teams
```

Credentials live in the `iac-cartographer/teams` secret as
`{"url": "https://prod-XX.westeurope.logic.azure.com:443/workflows/..."}`.
The workflow URL embeds a SAS token, so never check it into
version-controlled config.

Severity maps to Adaptive Card colours so messages are visually
distinguishable in the Teams channel:

| Level | Card colour | Emoji prefix |
|---|---|---|
| `info` | `good` (green) | ✅ |
| `warn` | `warning` (amber) | ⚠️ |
| `error` | `attention` (red) | ❌ |

Unicode emojis are used in the header text rather than Slack-style
`:emoji:` shortcodes — Teams does NOT render the shortcode form.

## Email (SMTP)

Sends operator-facing email — multipart/alternative with an HTML body
that renders severity as a coloured header and a plain-text fallback
for terminal mail clients. Tuned for the inbox shape: scannable
subject (`[iac-cartographer][ERROR] kaboom…`), full message inside the
body.

```yaml
notifications:
  - kind: email
    smtp_host: "smtp.sendgrid.net"
    smtp_port: 587                          # default
    from_address: "iac-cartographer@example.com"
    to_addresses:
      - "ops@example.com"
      - "devops@example.com"
    use_tls: true                           # default; only false for in-cluster relays
    subject_prefix: "[iac-cartographer]"    # default; override for multi-deployment inboxes
    levels: [warn, error]                   # default = all three
```

**Requires `pip install 'iac-cartographer[email]'`** — pulls in
`aiosmtplib` for async SMTP. If the dep is missing the channel logs
and skips (a misconfigured channel does NOT sink the run).

Credentials live in the `iac-cartographer/email` secret as
`{"username": "...", "password": "..."}`. Provider quirks:

| Provider | `username` | `password` |
|---|---|---|
| AWS SES | SES SMTP-credential username (NOT your IAM access key) | SES SMTP-credential password |
| SendGrid | the literal string `apikey` | your SendGrid API key |
| Postmark | your server token | the same server token |
| Mailgun | your SMTP login | your SMTP password |
| Internal Postfix relay | `username` configured on the relay | matching password |

Transport: STARTTLS on port 587 (modern SMTP submission). Port 465
(legacy implicit TLS) is **not** supported — open an issue if you need
it.

## AWS SNS

Publishes pipeline events to an SNS topic. SNS handles downstream
fanout — subscribe email, SMS, Lambda, SQS, HTTPS endpoints, and
mobile-push channels to the same topic from one place. Particularly
useful in AWS-first deployments where SNS already wires multiple
notification flows.

```yaml
notifications:
  - kind: sns
    topic_arn: "arn:aws:sns:eu-central-1:123456789012:iac-cartographer-events"
    region: "eu-central-1"                  # optional; defaults to boto3 chain
    levels: [error]                         # e.g. errors-only escalation
```

**Identity-based — no `iac-cartographer/sns` secret.** Auth comes from
the standard AWS credential chain (env vars, instance profile, IRSA /
workload identity on EKS, IAM role on ECS task). The principal needs
`sns:Publish` on the topic ARN. Same zero-secret-rotation experience
the Bedrock LLM backend has.

Each message carries two `MessageAttributes`:

```
level:  String  =  "info" | "warn" | "error"
source: String  =  "iac-cartographer"
```

SNS filter policies can route per-severity downstream — e.g. an email
subscription that fires only when `level=error`, plus a Lambda
subscription that fires on all three for archival. The SNS `Subject`
field carries `[iac-cartographer][LEVEL] {first 60 chars}` (capped at
the SNS 100-char limit) so inbox-style subscribers stay scannable.

Transport: `boto3` (already a base install dependency for AWS Secrets
Manager / SSM / Bedrock). The SNS client is synchronous, so `publish()`
runs in a thread via `asyncio.to_thread()` to keep the dispatcher's
concurrent fanout from blocking on the network round-trip.

## PagerDuty

Triggers PagerDuty incidents via the public **Events API v2**
(`events.pagerduty.com/v2/enqueue`). Authenticated by a **routing
key** (per-Service integration key) rather than a user token — one
channel entry = one PagerDuty Service.

```yaml
notifications:
  - kind: pagerduty
    levels: [error]                         # strongly recommended
```

> **Narrow to `levels: [error]`.** The channel does NOT enforce this —
> if you leave the default (all three), every info-level
> `iac-cartographer: run starting` will trigger an incident. That's
> probably not what you want.

Credentials live in the `iac-cartographer/pagerduty` secret as
`{"routing_key": "..."}`. Get the routing key from PagerDuty's
**Service → Integrations → Events API v2 → Integration Key**.

Severity mapping (level → PagerDuty `severity` field):

| Level | PagerDuty severity |
|---|---|
| `info` | `info` |
| `warn` | `warning` |
| `error` | `error` |

The channel deliberately maps the highest level to `error` rather than
`critical` — escalation belongs to the operator (via the `levels:`
filter or PagerDuty's own escalation policy), not the adapter.

Today the channel sends `event_action: "trigger"` only. Acknowledge /
resolve workflows would need a stable `dedup_key` per incident; not
shipped yet, would be a small follow-up.

## Opsgenie

Creates alerts via the public **Opsgenie Alerts API**. Authenticated
by a team / integration **API key** in an `Authorization: GenieKey
<key>` header (the Opsgenie-specific scheme — not `Bearer`).

```yaml
notifications:
  - kind: opsgenie
    region: "us"                            # or "eu" — match your key
    levels: [error]                         # strongly recommended
```

Same routing-policy advice as PagerDuty: narrow to `levels: [error]`
for page-on-error behaviour. The default `[info, warn, error]` will
generate an alert for every notification.

Credentials live in the `iac-cartographer/opsgenie` secret as
`{"api_key": "..."}`. Issued from a team integration or API
integration in the Opsgenie console.

**Region split.** Opsgenie maintains two independent control planes:

| `region` | Host | When to use |
|---|---|---|
| `us` *(default)* | `api.opsgenie.com` | Most accounts. |
| `eu` | `api.eu.opsgenie.com` | EU-resident accounts only. |

The two planes are **not linked** — a US-issued key is rejected by the
EU host and vice-versa. If unsure, check the URL you log into
(`app.opsgenie.com` = US, `app.eu.opsgenie.com` = EU).

Severity mapping (level → Opsgenie `priority` field):

| Level | Opsgenie priority |
|---|---|
| `info` | `P5` *(lowest — typically silent queue)* |
| `warn` | `P3` |
| `error` | `P1` *(page on-call)* |

The `details.level` field on every alert carries our original level
literal too, so Opsgenie filter rules can route by exact severity if
priority isn't granular enough.

## Slack (concrete reference)

The Slack channel posts via `chat.postMessage` with a bot token. The
bot must be invited to the destination channel (`#name` or `C0...`
both work).

```yaml
notifications:
  - kind: slack
    channel: "#alerts"        # falls back to top-level slack.channel when unset
    levels: [info, warn, error]
```

Credentials come from the `iac-cartographer/slack` secret (unchanged
shape):

```json
{"bot_token": "xoxb-..."}
```

Each post is prefixed with a single-character emoji matching its
severity (`:white_check_mark:` / `:warning:` / `:x:`) — terse so the
message text gets the screen real estate.

## What gets sent

| Event | Level | When |
|---|---|---|
| `iac-cartographer: run starting` | info | Pipeline start. |
| `iac-cartographer: run complete — N repos published` | info | Happy-path end. |
| `iac-cartographer: preflight failed — ...` | error | Confluence parent unreachable, etc. |
| `iac-cartographer: discovery failed — ...` | error | Every discovery source failed. |
| `iac-cartographer: no repos to process after filtering` | error | Discovery returned empty after deny-list. |
| `iac-cartographer: publish errors — ...` | error | One or more child pages failed to publish. |
| `:warning: Narrative review needed (AI-H1...)` | warn | Prompt-injection trigger detected in a narrative. Structural facts publish unchanged. |

## When to use which routing shape

- **One Slack channel, all severities.** Keep the legacy `slack:`
  block. Nothing to migrate.
- **Multiple Slack channels (info vs alerts).** Switch to
  `notifications:` with two `kind: slack` entries — different `channel`
  and `levels` per entry.
- **Chat + pager.** `notifications:` with `kind: slack` at all levels
  and a future `kind: pagerduty` at `levels: [error]`. Coming as a
  follow-up PR.
- **CI / air-gapped runs that need zero outbound calls.** Leave
  `notifications:` empty AND don't load a Slack secret. The dispatcher
  becomes a no-op — every `notifier.info(...)` is a silent return.
