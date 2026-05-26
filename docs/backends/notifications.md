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
| `email` | Coming next | SMTP via `aiosmtplib`. |
| `sns` | Coming next | AWS SNS topic publish — fits the existing AWS-first deployment story. |
| `pagerduty` / `opsgenie` | Coming next | Errors-only escalation. |
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
