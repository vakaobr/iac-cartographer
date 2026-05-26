"""Pydantic v2 data contracts for iac-cartographer.

Three groups of models:

  1. *Domain* — `RepoMetadata`, `TerraformSummary`, `BedrockNarrative`,
     `RepoInventory`, `RunOutcome`. The pipeline's actual data flow.
  2. *Config* — `AppConfig` and its sub-sections. Loaded once per run from
     SSM (or a local YAML file for development).
  3. *Credentials* — one model per Secrets Manager entry. Loaded once per run.

All models are strict (`extra="forbid"`) so a malformed config or upstream
JSON shape change blows up loudly during `model_validate*` rather than
producing a silent partial-publish.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves field types at validation time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    """Base for every model in this module — rejects unknown fields.

    The renderer must not silently drop new terraform-docs fields; the CLI
    must not silently ignore typos in `config.yaml`. Strict-by-default makes
    those failures loud.
    """

    model_config = ConfigDict(extra="forbid")


# ─── Discovery ─────────────────────────────────────────────────────────────


class RepoMetadata(_Strict):
    # Supported VCS hosts. `"other"` covers repos loaded via the file
    # source whose origin isn't one of the first-party-supported hosts.
    host: Literal["gitlab", "github", "bitbucket", "other"]
    full_name: str
    clone_url: str
    web_url: str
    default_branch: str
    last_commit_sha: str
    last_commit_at: datetime
    # Surface the last-commit author on the published page so a reader can
    # trace a suspicious narrative back to the contributor who touched the
    # source repo (defense-in-depth against indirect prompt injection via
    # commit messages or README content). `None` when the API didn't return
    # one (rare; e.g. orphaned commits).
    last_commit_author: str | None = None


# ─── terraform-docs JSON (subset we render + send to Bedrock) ──────────────


class ProviderRef(_Strict):
    name: str
    source: str | None = None
    version: str | None = None
    alias: str | None = None


class ModuleRef(_Strict):
    name: str
    source: str
    version: str | None = None


class ResourceRef(_Strict):
    type: str
    name: str
    mode: Literal["managed", "data"] = "managed"
    provider: str | None = None


class VariableRef(_Strict):
    name: str
    type: str | None = None
    description: str | None = None
    default: object | None = None
    required: bool = False


class OutputRef(_Strict):
    name: str
    description: str | None = None


class TerraformSummary(_Strict):
    providers: list[ProviderRef] = Field(default_factory=list)
    requirements: dict[str, str] = Field(default_factory=dict)
    modules: list[ModuleRef] = Field(default_factory=list)
    resources: list[ResourceRef] = Field(default_factory=list)
    inputs: list[VariableRef] = Field(default_factory=list)
    outputs: list[OutputRef] = Field(default_factory=list)
    resource_counts_by_type: dict[str, int] = Field(default_factory=dict)
    # Relative paths (from repo root) of every directory the extractor ran
    # terraform-docs in. Empty for repos with a single root-level module;
    # populated to e.g. `["terraform/env/dev", "terraform/env/staging",
    # "terraform/env/prod"]` for op-infrastructure-style multi-env layouts.
    # Three load-bearing reasons to surface this on the model:
    #   1. Sonnet gets it in the narrator prompt as a `<module-paths>` block
    #      so environment detection no longer depends on guessing from
    #      inline HCL hints.
    #   2. It renders directly on the Confluence child page (operators see
    #      the actual repo layout without cloning).
    #   3. It participates in `compute_sha` — adding/removing a module dir
    #      now invalidates the banner-SHA and triggers a republish.
    module_paths: list[str] = Field(default_factory=list)


# ─── Bedrock narrative (strict; validated post-invoke) ─────────────────────


class ResourceExplanation(_Strict):
    # MUST appear in TerraformSummary.resources — verified at the call site
    # before the narrative is accepted (prevents the model inventing types).
    resource_type: str
    why_it_exists: str = Field(max_length=400)


class BedrockNarrative(_Strict):
    purpose: str = Field(min_length=20, max_length=600)
    key_resources_explained: list[ResourceExplanation] = Field(default_factory=list, max_length=12)
    environments: list[str] = Field(default_factory=list)
    owning_team_guess: str | None = None
    notable_patterns: list[str] = Field(default_factory=list, max_length=8)

    # Reject URLs in narrative free-text fields — defense against indirect
    # prompt injection where an attacker tries to make the model emit a
    # phishing link into the published page. ADF doesn't auto-link plain
    # text, but copy-paste is still a risk. On validation failure the
    # narrator's retry-once-then-skip path handles the error gracefully
    # (narrative=None; structural facts still publish).
    @field_validator("purpose")
    @classmethod
    def _purpose_no_urls(cls, v: str) -> str:
        if "http://" in v.lower() or "https://" in v.lower():
            raise ValueError("purpose may not contain URLs")
        return v

    @field_validator("notable_patterns")
    @classmethod
    def _notable_patterns_no_urls(cls, v: list[str]) -> list[str]:
        for item in v:
            if "http://" in item.lower() or "https://" in item.lower():
                raise ValueError("notable_patterns may not contain URLs")
        return v


# ─── Composite ─────────────────────────────────────────────────────────────


class RepoInventory(_Strict):
    meta: RepoMetadata
    summary: TerraformSummary
    # None if Bedrock failed for this repo; the page still renders structural
    # facts in that case (per ADR-005 §Consequences).
    narrative: BedrockNarrative | None = None


# ─── Run outcome (aggregate; emitted to logs + CloudWatch + Slack) ─────────


class RunOutcome(_Strict):
    discovered: int = 0
    succeeded: int = 0
    skipped_unchanged: int = 0
    failed: dict[str, str] = Field(default_factory=dict)
    pages_updated: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    bedrock_tokens_in: int = 0
    bedrock_tokens_out: int = 0


# ─── Config (loaded from SSM Parameter Store at startup) ───────────────────


class DiscoveryConfig(_Strict):
    # GitLab group IDs whose subtree (incl. subgroups) should be scanned for
    # `*.tf` files. Empty list = skip GitLab.
    gitlab_group_ids: list[int] = Field(default_factory=list)
    # GitHub organisations to scan via `code search`. Empty list = skip GitHub.
    github_orgs: list[str] = Field(default_factory=list)
    # Bitbucket Cloud workspaces to enumerate. Empty list = skip Bitbucket.
    # The source lists every repo in the workspace (Bitbucket's public API
    # has no `extension:tf`-style filter on free plans) — combine with
    # `deny_repos` to narrow the scope.
    bitbucket_workspaces: list[str] = Field(default_factory=list)
    # Optional path to a YAML/JSON file containing a hand-curated list of
    # `RepoMetadata` records. Loaded as an additional `DiscoverySource`;
    # combine with the VCS-host fields or use standalone for air-gapped
    # runs. See `iac_cartographer/discovery/file.py` for the schema.
    repos_file: str | None = None
    # Glob patterns (against full_name) to exclude from publishing — e.g.
    # `*-archived`, `examples/*`, `vendor-*`.
    deny_repos: list[str] = Field(default_factory=list)
    # Optional override for the owning-team guess: full_name → team string.
    # Useful when team mapping isn't trivially derivable from the repo path.
    owner_overrides: dict[str, str] = Field(default_factory=dict)
    # Self-hosted GitLab base URL (without `/api/v4` suffix). Override to point
    # at gitlab.example.com; defaults to gitlab.com.
    gitlab_base_url: str = "https://gitlab.com"


class LLMConfig(_Strict):
    """Configuration for the LLM that writes the per-repo narrative.

    `backend` picks the provider; the rest of the fields are interpreted in
    that backend's namespace. Adding a new backend means:
      * Add a literal to the `backend` discriminator below.
      * Add an `LLMBackend` subclass in `llm.py`.
      * Wire the cli's secrets-loading + backend instantiation in `cli.py`.

    `BedrockConfig` is preserved as an alias of this class for back-compat
    with code that referenced the old name during the internal phase.
    """

    # Which LLM provider to use.
    #   "bedrock"      → AWS Bedrock InvokeModel (auth via the standard
    #                    AWS credential chain — env vars, instance
    #                    profile, IRSA, etc.). Default.
    #   "anthropic"    → Anthropic API direct (auth via an API key in
    #                    the `iac-cartographer/anthropic` secret).
    #   "vertex"       → Claude on Vertex AI / Google Cloud (auth via
    #                    GCP Application Default Credentials).
    #                    Requires `pip install iac-cartographer[gcp]`.
    #   "azure_openai" → GPT family on Azure OpenAI (auth via API key
    #                    in `iac-cartographer/azure_openai` secret, OR
    #                    Azure AD / managed identity when
    #                    azure_openai_use_aad is true).
    #                    Requires `pip install iac-cartographer[azure]`.
    #   "openai"       → GPT family via api.openai.com (or any
    #                    OpenAI-compatible gateway via openai_base_url).
    #                    Auth via API key in `iac-cartographer/openai`.
    #                    Requires `pip install iac-cartographer[openai]`.
    #   "ollama"       → Local LLM via Ollama's native /api/chat
    #                    endpoint. Zero auth by default (server bound
    #                    to localhost). No extra optional dependency.
    backend: Literal["bedrock", "anthropic", "vertex", "azure_openai", "openai", "ollama"] = "bedrock"

    # Model identifier — meaning is backend-specific.
    #   bedrock: an inference-profile ID (e.g. `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`)
    #   anthropic: a model name (e.g. `claude-sonnet-4-5-20250929`)
    # The default here is a Bedrock inference-profile that works on the
    # default backend; override when you flip backends.
    model_id: str = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

    # Max output tokens per invocation. Same meaning across backends.
    max_tokens: int = 4096

    # Increments when the system prompt changes — invalidates banner-SHA
    # history so all pages get a forced republish on the next run.
    system_prompt_version: str = "v1"

    # Bedrock-only: AWS region for the boto3 client.
    bedrock_region: str = "eu-central-1"

    # Anthropic-only: API base URL. Override to point at a proxy (e.g.
    # `https://api.anthropic.example/v1` if you front the Anthropic API
    # with an internal gateway).
    anthropic_base_url: str = "https://api.anthropic.com"

    # Vertex-only: GCP project ID hosting the Vertex AI Claude endpoint.
    # Default empty so the model still validates with default settings
    # (matching the `bedrock` + `anthropic` shape); required when
    # backend=vertex — the cli's `_build_llm_backend` raises a clean
    # ConfigError if it's missing.
    vertex_project_id: str = ""

    # Vertex-only: Vertex AI region (e.g. `europe-west1`, `us-east5`).
    # Pick a region where Claude is available — see
    # https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude
    # for the current list.
    vertex_region: str = "europe-west1"

    # Azure OpenAI-only: resource endpoint, e.g.
    # `https://my-resource.openai.azure.com/`. Required when
    # backend=azure_openai.
    azure_openai_endpoint: str = ""

    # Azure OpenAI-only: deployment NAME (NOT the underlying model —
    # Azure decouples them via the Studio UI). Required when
    # backend=azure_openai. `model_id` is ignored for this backend
    # because Azure routes by deployment, not by model name.
    azure_openai_deployment: str = ""

    # Azure OpenAI-only: API version. Bump as Azure releases new
    # versions with features (structured outputs, etc.).
    azure_openai_api_version: str = "2024-10-21"

    # Azure OpenAI-only: skip the `iac-cartographer/azure_openai` secret
    # and authenticate via Azure AD / managed identity instead. Picks up
    # workload identity in cluster, IMDS on Azure VMs, or `az login` ADC
    # for local dev. Recommended for cloud-native deployments — no
    # secret to rotate.
    azure_openai_use_aad: bool = False

    # OpenAI-only: API base URL. Override to point at an OpenAI-compatible
    # gateway / proxy (LiteLLM, Azure API Management routes, internal
    # LLM gateway). The SDK defaults to `https://api.openai.com/v1`.
    openai_base_url: str = "https://api.openai.com/v1"

    # OpenAI-only: org ID. Most accounts don't need this; set when your
    # billing routes through a specific org and the default doesn't.
    openai_organization: str | None = None

    # Ollama-only: server URL. Defaults to Ollama's standard local
    # bind. Set to a remote host (`http://ollama.internal:11434`) for
    # shared deployments, optionally with `ollama_extra_headers` for
    # reverse-proxy auth.
    ollama_base_url: str = "http://localhost:11434"

    # Ollama-only: per-invocation timeout. Local CPU inference can be
    # slow on big models; default is 5 min, override for tighter SLOs
    # or much longer ones.
    ollama_timeout_seconds: float = 300.0

    # Ollama-only: extra request headers (e.g. for a reverse-proxy
    # bearer token). Plain map of strings; merged into the request
    # headers as-is.
    ollama_extra_headers: dict[str, str] = Field(default_factory=dict)


# Back-compat alias. The original internal code used `BedrockConfig`; new
# code should use `LLMConfig`. Remove this alias after a release cycle.
BedrockConfig = LLMConfig


class ConfluenceConfig(_Strict):
    # Atlassian Cloud site without protocol or trailing slash.
    # Example: "your-org.atlassian.net". The placeholder is obviously invalid
    # in production — Confluence requests will fail loudly with DNS errors
    # if left unset — but it keeps the model validatable for tests/dry-runs.
    site: str = "your-org.atlassian.net"
    # Confluence space key (e.g. "DOCS", "Engineering"). The parent page must
    # already exist in this space; iac-cartographer publishes child pages under it.
    space_key: str = "DOCS"
    # Logical name of the parameter holding the parent page's numeric ID
    # as a plain string. Resolved via the configured `SecretsProvider`
    # (`get_parameter()`):
    #   * AWS:   SSM Parameter Store path — same as the original
    #            behaviour (`/iac-cartographer/confluence-parent-id`).
    #   * env:   env var `IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID`.
    #   * vault: `{mount}/data/iac-cartographer/confluence-parent-id`
    #            with the page ID stored under a `value` field.
    # The parent page is the overview; child pages live under it.
    parent_page_id_ssm_path: str = "/iac-cartographer/confluence-parent-id"

    # Optional direct override. When set, the page ID is taken verbatim
    # from here and `parent_page_id_ssm_path` is ignored. Use for
    # deployments where storing a non-secret integer ID in an external
    # parameter store is overkill (small teams, file-based config, etc.).
    parent_page_id: str | None = None


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
    """Stdout / stderr JSON Lines notification channel.

    Emits one JSON line per notification to the configured stream.
    Useful for CI runs, air-gapped deployments, and local dev where
    chat / pager / SMTP destinations aren't available but a log
    aggregator picks up stdout.

    No credentials, no HTTP, no SDK — `print()` is the only I/O.
    """

    kind: Literal["stdout"] = "stdout"
    # `"stdout"` (default) or `"stderr"`. Use `stderr` when stdout
    # is reserved for machine-parseable pipeline output.
    stream: Literal["stdout", "stderr"] = "stdout"


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


class PublisherConfig(_Strict):
    """Selects WHERE the inventory gets published.

    Most fields are backend-specific and ignored when `kind` doesn't match.
    Adding a new publisher means:
      * Add a literal to the `kind` discriminator.
      * Add an `Publisher` subclass in `publishers/`.
      * Wire it in the cli's `_build_publisher` helper.
    """

    # Which publisher to use.
    #   "confluence" → publish ADF pages to Atlassian Confluence Cloud.
    #                  Uses `confluence:` config + the
    #                  `iac-cartographer/confluence` secret. Default.
    #   "markdown"   → write Markdown files to a local directory. Uses
    #                  the `markdown:` config. No credentials needed.
    #   "html"       → write self-contained HTML files (embedded CSS, no
    #                  external dependencies) to a local directory. Uses
    #                  the `html:` config. No credentials needed. Designed
    #                  for snapshots, S3/CloudFront hosting, audit PDFs.
    #   "json"       → write machine-readable JSON files to a local
    #                  directory. Uses the `json:` config. Designed as a
    #                  feed for Backstage catalogs, internal CMDBs,
    #                  dashboards, and custom drift-detection tooling.
    kind: Literal["confluence", "markdown", "html", "json"] = "confluence"


class MarkdownConfig(_Strict):
    """`publisher.kind == "markdown"` settings.

    Output layout under `output_dir`:

        output_dir/
        ├── index.md
        └── repos/
            └── <full_name_slugged>.md
    """

    output_dir: str = "./iac-inventory"


class HtmlConfig(_Strict):
    """`publisher.kind == "html"` settings.

    Output layout under `output_dir`:

        output_dir/
        ├── index.html
        └── repos/
            └── <full_name_slugged>.html

    Each file is self-contained (embedded CSS, no JS, no external fonts)
    so it works opened directly from disk, mailed as an attachment, or
    uploaded to S3 + CloudFront / GitHub Pages without a build step.
    """

    output_dir: str = "./iac-inventory-html"


class JsonConfig(_Strict):
    """`publisher.kind == "json"` settings.

    Output layout under `output_dir`:

        output_dir/
        ├── index.json
        └── repos/
            └── <full_name_slugged>.json

    The overview (`index.json`) is suitable as a feed for Backstage
    catalog imports, internal CMDBs, or dashboards — it includes a row
    per repo with key metadata + aggregate counts. The per-repo files
    carry the full `RepoInventory` payload (providers, modules,
    resources, inputs, outputs, narrative).

    Top-level `iac_cartographer.sha` field carries the banner-SHA so
    the publisher's idempotent-republish short-circuit works the same
    way as the Markdown / HTML / Confluence publishers.
    """

    output_dir: str = "./iac-inventory-json"


class SecretsConfig(_Strict):
    """Selects WHERE credentials + opaque parameters come from.

    Most fields are backend-specific and ignored when `backend` doesn't
    match. Adding a new backend means: add a literal to the discriminator,
    implement the subclass in `secrets/`, and add a branch to
    `secrets.build_provider`.
    """

    # Which secrets backend to use.
    #   "aws"   → AWS Secrets Manager + SSM Parameter Store (default; what
    #             the production deployment iac-cartographer was extracted
    #             from uses).
    #   "env"   → Process environment variables. Naming convention:
    #             `IAC_CARTOGRAPHER_SECRET_<NAME>` for secrets (JSON value),
    #             `IAC_CARTOGRAPHER_PARAM_<NAME>` for opaque parameters.
    #             Optional `.env` autoload via `env_dotenv_path`.
    #   "vault" → HashiCorp Vault KV v2 over HTTP. Auth via VAULT_TOKEN env.
    backend: Literal["aws", "env", "vault"] = "aws"

    # AWS region for boto3 clients when backend == "aws". Ignored otherwise.
    aws_region: str = "eu-central-1"

    # Path to a `.env` file to autoload before reading env vars
    # (backend == "env" only). Pre-existing env vars take precedence.
    # `None` = don't autoload.
    env_dotenv_path: str | None = None

    # Vault server URL when backend == "vault" (e.g. `https://vault.example.com`).
    vault_addr: str = ""

    # KV v2 mount path (Vault terminology — see `vault read -mount`).
    vault_mount: str = "secret"

    # Logical prefix joined under the mount. Leave default if you mirror the
    # `iac-cartographer/...` naming convention; override to a flat path if
    # the operator strips the prefix at the Vault layer.
    vault_path_prefix: str = "iac-cartographer/"

    # Vault Enterprise namespace header. None = single-tenant Vault.
    vault_namespace: str | None = None


class AppConfig(_Strict):
    # `populate_by_name=True` lets the YAML key stay `json:` while the
    # Python attribute is renamed to `json_output` (avoiding the
    # shadow-warning on Pydantic's deprecated `.json()` method).
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    # The YAML section is named `llm:`. The previous internal name was
    # `bedrock:` — operators migrating from a pre-1.0 deployment must
    # rename that section. The schema is otherwise unchanged.
    llm: LLMConfig = Field(default_factory=LLMConfig)
    # `publisher:` picks which backend ("confluence" or "markdown") and
    # only the matching sub-config matters. `confluence:` and `markdown:`
    # stay top-level (not nested under `publisher:`) so we can default
    # them both and let the runtime ignore the irrelevant one.
    publisher: PublisherConfig = Field(default_factory=PublisherConfig)
    # `secrets:` picks where credentials + opaque parameters (Confluence
    # parent page ID, etc.) come from. Default is the legacy AWS pair
    # (Secrets Manager + SSM); override to `env` or `vault` for
    # non-AWS deployments.
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    confluence: ConfluenceConfig = Field(default_factory=ConfluenceConfig)
    markdown: MarkdownConfig = Field(default_factory=MarkdownConfig)
    html: HtmlConfig = Field(default_factory=HtmlConfig)
    # YAML key is `json:` — Python attribute is `json_output` to avoid
    # shadowing Pydantic v2's deprecated `BaseModel.json()` shim.
    json_output: JsonConfig = Field(default_factory=JsonConfig, alias="json")
    slack: SlackConfig = Field(default_factory=SlackConfig)
    # Multi-channel notifications. When non-empty, the dispatcher fans
    # every pipeline event out to each listed channel concurrently and
    # honours each entry's own `levels:` filter. When empty (default),
    # the dispatcher falls back to the legacy single-Slack shape — the
    # top-level `slack:` block + `iac-cartographer/slack` secret act
    # as if they were the sole entry at all three levels. Migration is
    # opt-in: add a `notifications:` list when you need a second
    # destination, otherwise leave it empty.
    notifications: list[NotificationConfig] = Field(default_factory=list)


# ─── Secrets (one model per Secrets Manager entry) ─────────────────────────


class ConfluenceCredentials(_Strict):
    email: str
    api_token: str


class AnthropicCredentials(_Strict):
    """Anthropic API key for the `anthropic` LLM backend. Loaded only when
    `llm.backend == "anthropic"` — Bedrock deployments don't need it."""

    api_key: str


class AzureOpenAICredentials(_Strict):
    """Azure OpenAI API key for the `azure_openai` LLM backend. Loaded only
    when `llm.backend == "azure_openai"` AND `llm.azure_openai_use_aad` is
    false. AAD-authenticated deployments skip this secret entirely (auth
    flows through workload identity / managed identity instead)."""

    api_key: str


class OpenAICredentials(_Strict):
    """OpenAI API key for the `openai` LLM backend. Loaded only when
    `llm.backend == "openai"`."""

    api_key: str


class GitlabCredentials(_Strict):
    token: str


class GithubCredentials(_Strict):
    token: str


class BitbucketCredentials(_Strict):
    """Bitbucket Cloud credentials. Set EITHER `access_token` (recommended —
    workspace access tokens are scoped to one workspace) OR `username` +
    `app_password` (legacy form, still widely used).

    The model_validator below enforces the XOR so misconfigured secrets
    surface at load time instead of as a 401 mid-pipeline."""

    access_token: str | None = None
    username: str | None = None
    app_password: str | None = None

    @model_validator(mode="after")
    def _exactly_one_auth_mode(self) -> BitbucketCredentials:
        has_token = self.access_token is not None
        has_basic = self.username is not None and self.app_password is not None
        if has_token == has_basic:
            raise ValueError(
                "BitbucketCredentials: set EITHER access_token OR (username + app_password), not both/neither"
            )
        # If basic is partially set (only one of the two), surface it clearly.
        if not has_token and (self.username is None) != (self.app_password is None):
            raise ValueError("BitbucketCredentials: username and app_password must be set together")
        return self


class SlackCredentials(_Strict):
    bot_token: str
    # The channel always comes from `SlackConfig.channel` (SSM-backed); this
    # field is retained for back-compat with older secret payloads but is no
    # longer required. The SlackNotifier ignores it when the orchestrator
    # passes `channel=config.slack.channel` (it always does).
    channel_id: str | None = None


# ─── Webhook-family notification credentials ─────────────────────────
# All three share the same shape (just a URL) but stay separate so the
# logical secret name is encoded in the type — same pattern the LLM
# credential classes follow (Anthropic / OpenAI / AzureOpenAI all have
# a single `api_key: str` but live as distinct types).


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
