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
    #   "bedrock"   → AWS Bedrock InvokeModel (auth via the standard AWS
    #                 credential chain — env vars, instance profile,
    #                 IRSA, etc.). Default.
    #   "anthropic" → Anthropic API direct (auth via an API key loaded
    #                 from the `iac-cartographer/anthropic` secret).
    #   "vertex"    → Claude on Vertex AI / Google Cloud (auth via GCP
    #                 Application Default Credentials — workload identity
    #                 in cluster, ADC for local dev, SA key for batch
    #                 jobs). Requires `pip install iac-cartographer[gcp]`.
    backend: Literal["bedrock", "anthropic", "vertex"] = "bedrock"

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


# ─── Secrets (one model per Secrets Manager entry) ─────────────────────────


class ConfluenceCredentials(_Strict):
    email: str
    api_token: str


class AnthropicCredentials(_Strict):
    """Anthropic API key for the `anthropic` LLM backend. Loaded only when
    `llm.backend == "anthropic"` — Bedrock deployments don't need it."""

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
