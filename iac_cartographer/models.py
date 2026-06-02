"""Pydantic v2 data contracts for iac-cartographer.

This module holds the **shared / cross-cutting domain models** — the
pipeline's actual data flow (`RepoMetadata`, `TerraformSummary`,
`BedrockNarrative`, `RepoInventory`, `RunOutcome`) — plus the top-level
`AppConfig` aggregator that composes each subsystem's config.

Each subsystem's own config + credential models now live beside that
subsystem so the "one concern, one package" rule holds:

  * `discovery/config.py`     — `DiscoveryConfig` + VCS-host credentials
  * `llm_config.py`           — `LLMConfig` + LLM-provider credentials
  * `publishers/config.py`    — per-publisher configs + their credentials
  * `secrets/config.py`       — `SecretsConfig`
  * `notifications/config.py` — channel configs + notification credentials

Those symbols are **re-exported from this module** (see the block at the
bottom) so existing `from iac_cartographer.models import X` import sites
keep working unchanged. This is an intentional internal seam, not an
external back-compat shim.

All models are strict (`extra="forbid"`) so a malformed config or upstream
JSON shape change blows up loudly during `model_validate*` rather than
producing a silent partial-publish.
"""

from __future__ import annotations

import warnings
from datetime import datetime  # noqa: TC003 — Pydantic resolves field types at validation time
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


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
    # `"gitea"` covers both Gitea and Forgejo (Forgejo forked from
    # Gitea in 2022 and intentionally preserves API + auth-scheme
    # compatibility, so one discovery source + one auth path handles
    # both).
    host: Literal["gitlab", "github", "bitbucket", "gitea", "other"]
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


class StateBackendSignal(_Strict):
    """One safety-relevant assertion about a state backend.

    `label` is the short noun phrase shown in the rendered table
    ("Encryption", "State locking", "KMS key", "Backend"). `value` is
    the human-readable status ("enabled", "missing", "AWS-managed
    SSE-S3", "local-disk"). `severity` drives icon + color:

      * `ok`       — configured the recommended way
      * `info`     — configured a non-default but valid way
      * `warn`     — a recommended setting is missing
      * `critical` — actively dangerous (e.g. a local backend in a
                     production-shaped repo).

    The renderer picks an icon per severity at render time; this model
    deliberately doesn't bake unicode glyphs into the data layer.
    """

    label: str
    value: str
    severity: Literal["ok", "info", "warn", "critical"] = "info"


class StateBackend(_Strict):
    """One `terraform { backend "TYPE" { ... } }` block, parsed from HCL.

    Backend type identifies the storage mechanism (`s3`, `gcs`,
    `azurerm`, `remote`, `local`, `consul`, `etcdv3`, `http`,
    `kubernetes`, `pg`). `attrs` holds the raw key/value pairs from the
    block (strings only — no type coercion, since the renderer only
    reads them and Pydantic can't validate provider-specific shapes
    without growing one model per backend type).

    `signals` is the precomputed posture summary — derived from `attrs`
    at parse time so the renderer doesn't need to know per-backend
    safety logic. `module_path` is the repo-relative directory the
    block was declared in (`"."` for a root-level module), letting the
    renderer surface one row per backend in repos that declare several
    (e.g. an op-infrastructure-style `env/dev` + `env/prod` layout).
    """

    module_path: str
    type: str
    attrs: dict[str, str] = Field(default_factory=dict)
    signals: list[StateBackendSignal] = Field(default_factory=list)


class TerraformSummary(_Strict):
    providers: list[ProviderRef] = Field(default_factory=list)
    requirements: dict[str, str] = Field(default_factory=dict)
    modules: list[ModuleRef] = Field(default_factory=list)
    resources: list[ResourceRef] = Field(default_factory=list)
    inputs: list[VariableRef] = Field(default_factory=list)
    outputs: list[OutputRef] = Field(default_factory=list)
    resource_counts_by_type: dict[str, int] = Field(default_factory=dict)
    # One entry per `terraform { backend "..." { ... } }` block found.
    # Zero entries → the repo doesn't declare a remote backend, or only
    # declares one in a path we can't parse — the page omits the
    # section in that case. Multiple entries → one per module_path
    # (op-infrastructure-style envs), or rarely a duplicate declaration
    # the operator should clean up.
    state_backends: list[StateBackend] = Field(default_factory=list)
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


# ─── Live state overlay (TFC / Terrakube / future) ─────────────────────────


class LiveStateInfo(_Strict):
    """One workspace's live state as observed at run time.

    External — every field can change without any code change in the repo
    being indexed. Excluded from the banner-SHA payload (`_inventory_input_payload`
    in `renderer.py`) for that reason: hashing it would invalidate the page
    on every run regardless of whether structural facts changed.
    """

    workspace_name: str
    workspace_url: str
    # Current-run shape. Optional because a workspace can exist with no
    # runs (newly created, paused, etc.).
    current_run_status: str | None = None
    current_run_id: str | None = None
    current_run_url: str | None = None
    last_successful_apply_at: datetime | None = None
    # `"drift_detected"` / `"no_drift"` / `"not_configured"` — TFC's
    # drift-detection feature is opt-in per workspace; surface
    # `not_configured` rather than guessing when assessment data is
    # absent.
    drift_status: Literal["drift_detected", "no_drift", "not_configured"] = "not_configured"
    # Live resource count from the platform's API. Compared against
    # `summary.resources` length on the rendered page; a divergence
    # usually means the operator applied something outside Terraform.
    live_resource_count: int | None = None


class StaleApplyAlert(_Strict):
    """One stale failed-apply finding emitted as a `warn`-level notification.

    Collected during the live-state overlay's per-repo `fetch()` call —
    no separate poll loop — and dispatched at the end of the run via the
    existing notifications channel set.
    """

    workspace_name: str
    workspace_url: str
    failed_run_id: str
    failed_run_url: str
    days_in_state: float
    last_successful_apply_at: datetime | None = None


# ─── Composite ─────────────────────────────────────────────────────────────


class RepoInventory(_Strict):
    meta: RepoMetadata
    summary: TerraformSummary
    # None if Bedrock failed for this repo; the page still renders structural
    # facts in that case (per ADR-005 §Consequences).
    narrative: BedrockNarrative | None = None
    # Optional live-state overlay (TFC / Terrakube / future). Populated
    # by the orchestrator after extraction when `live_state.backend != "none"`.
    # `None` when no overlay is configured or no workspace maps to this
    # repo. NOT hashed into the banner-SHA — external state.
    live_state: LiveStateInfo | None = None


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


# ─── Re-exported subsystem config + credential models ──────────────────────
#
# Each subsystem's config + credential models now live in that subsystem's
# package (or, for the single-module `llm`, a sibling `llm_config.py`). They
# are imported here so `from iac_cartographer.models import X` keeps working
# everywhere it already did — an intentional internal seam.
#
# These imports MUST come after `_Strict` and the domain models above are
# defined: the subpackage `config.py` modules do `from iac_cartographer.models
# import _Strict`, so importing them earlier would hit a half-initialised
# module. Placing the block here means `_Strict` already exists when the cycle
# resolves.

from iac_cartographer.discovery.config import (  # noqa: E402
    BitbucketCredentials,
    DiscoveryConfig,
    GiteaCredentials,
    GithubCredentials,
    GitlabCredentials,
)
from iac_cartographer.llm_config import (  # noqa: E402
    AnthropicCredentials,
    AzureOpenAICredentials,
    BedrockConfig,
    LLMConfig,
    OpenAICredentials,
)
from iac_cartographer.notifications.config import (  # noqa: E402
    DiscordCredentials,
    DiscordNotificationConfig,
    EmailCredentials,
    EmailNotificationConfig,
    NotificationConfig,
    OpsgenieCredentials,
    OpsgenieNotificationConfig,
    PagerDutyCredentials,
    PagerDutyNotificationConfig,
    SlackConfig,
    SlackCredentials,
    SlackNotificationConfig,
    SlackWebhookCredentials,
    SlackWebhookNotificationConfig,
    SnsNotificationConfig,
    StdoutNotificationConfig,
    TeamsCredentials,
    TeamsNotificationConfig,
    WebhookCredentials,
    WebhookNotificationConfig,
)
from iac_cartographer.publishers.config import (  # noqa: E402
    ConfluenceConfig,
    ConfluenceCredentials,
    GitHubWikiConfig,
    HtmlConfig,
    JsonConfig,
    MarkdownConfig,
    NotionConfig,
    NotionCredentials,
    PublisherConfig,
)
from iac_cartographer.secrets.config import SecretsConfig  # noqa: E402

# ─── Config (assembled from each subsystem's section) ──────────────────────


class WorkspaceMappingRule(_Strict):
    """Map a repo `full_name` to a workspace name on the live-state platform.

    Both fields support `fnmatch`-style glob patterns. When a repo matches
    multiple rules the first match wins (rules are checked in list order).
    Empty `workspace_mapping` falls back to the default heuristic — the
    workspace name is the last segment of `repo.full_name` (e.g.
    `acme-org/main-cluster` → `main-cluster`)."""

    repo: str
    workspace: str


class StalenessConfig(_Strict):
    """Stale failed-apply alert thresholds — sub-feature of the live-state overlay."""

    # Default `true` so adopters who configure the overlay get the most
    # operationally interesting signal it produces without thinking about it.
    enabled: bool = True
    # Days a workspace can sit in `errored` state before we alert. 2 days
    # covers a long weekend; lower values get noisy, higher values risk
    # missing legitimately broken applies. Tune per team noise tolerance.
    threshold_days: int = 2
    # `fnmatch`-style patterns of workspace names to never alert on
    # (deliberately deferred work, decommissioning queue, etc.). Matched
    # against the workspace name, not the repo name — the same workspace
    # ought to be alertable or muted regardless of which repo points at it.
    acknowledged_stale: list[str] = Field(default_factory=list)


class LiveStateConfig(_Strict):
    """Live-state overlay configuration — layers workspace info from TFC
    (or a sibling backend) on top of the static inventory.

    Default is `backend: "none"`: no overlay, no extra credentials needed,
    no API calls. Adopters opt in by flipping `backend` to `tfc`
    (Terraform Cloud / HCP Terraform) or a future backend like
    `terrakube`."""

    # Backend selector. `"none"` is the no-op default; `"tfc"` covers
    # Terraform Cloud / HCP Terraform / self-hosted Terraform Enterprise;
    # `"terrakube"` covers self-hosted Terrakube (#99).
    backend: Literal["none", "tfc", "terrakube"] = "none"
    # Organisation name on the live-state platform. Required when
    # `backend != "none"`. For TFC this is the org slug as it appears in
    # `app.terraform.io/app/<org>/...`; for Terrakube this is the
    # organisation's display name (the overlay resolves it to a UUID
    # internally via the `/organization` endpoint at startup).
    organization: str = ""
    # API hostname. `app.terraform.io` covers TFC + HCP Terraform;
    # override for self-hosted Terraform Enterprise (e.g.
    # `tfe.acme.internal`) or Terrakube (e.g. `terrakube.acme.internal`).
    # The overlay constructs `https://<hostname>/api/v2/...` for TFC and
    # `https://<hostname>/api/v1/...` for Terrakube.
    hostname: str = "app.terraform.io"
    # Explicit per-repo → per-workspace mappings. Empty list = use the
    # default heuristic (workspace name = last segment of `repo.full_name`).
    workspace_mapping: list[WorkspaceMappingRule] = Field(default_factory=list)
    # Stale failed-apply alert sub-feature config.
    staleness: StalenessConfig = Field(default_factory=lambda: StalenessConfig())


class GraphConfig(_Strict):
    """Mermaid resource-dependency-graph rendering knobs.

    A single global threshold for now (chunking); follow-up issues
    (e.g. `depends_on` edge inference) can extend this block without
    breaking compatibility — every field has a default so omitted
    YAML sections still validate.
    """

    # Per-diagram resource-node cap. A single Mermaid diagram with
    # hundreds of nodes is unreadable; the renderer splits into chunks
    # of <= this many resources, with whole providers kept together
    # within a chunk (a single oversized provider ships as its own
    # chunk rather than splitting its resources). 25 was the threshold
    # called out on issue #95.
    max_nodes_per_graph: int = 25


class AppConfig(_Strict):
    # `populate_by_name=True` lets the canonical YAML key be `json_output:`
    # while still accepting the deprecated `json:` alias (see the
    # `json_output` field's AliasChoices + the deprecation validator below).
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _warn_deprecated_keys(cls, data: object) -> object:
        """Emit DeprecationWarning for pre-1.0 YAML keys that have been
        renamed but still validate via an alias. Keeps old configs working
        while nudging operators to the 1.0 names."""
        if isinstance(data, dict) and "json" in data and "json_output" not in data:
            warnings.warn(
                "config key `json:` is deprecated; rename it to `json_output:` "
                "(the old key still works for now and will be removed in 2.0)",
                DeprecationWarning,
                stacklevel=2,
            )
        return data

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
    # Canonical YAML key is `json_output:` (the Python attribute is also
    # `json_output`, to avoid shadowing Pydantic v2's deprecated
    # `BaseModel.json()` shim). The pre-1.0 `json:` key still validates via
    # AliasChoices but is deprecated — see `_warn_deprecated_keys` above.
    json_output: JsonConfig = Field(
        default_factory=JsonConfig,
        validation_alias=AliasChoices("json_output", "json"),
    )
    notion: NotionConfig = Field(default_factory=NotionConfig)
    github_wiki: GitHubWikiConfig = Field(default_factory=GitHubWikiConfig)
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
    # `graph:` controls the Mermaid resource-dependency diagram embedded
    # on each child page. The only knob today is the chunking threshold;
    # additional knobs (whether to render `depends_on` edges, etc.) can
    # extend this block in follow-up issues without touching `AppConfig`.
    graph: GraphConfig = Field(default_factory=lambda: GraphConfig())
    # `live_state:` layers external workspace info (TFC / HCP / Terrakube)
    # on top of the static inventory. Default backend is `"none"` — no
    # API calls, no credential, no behaviour change for existing
    # deployments. Adopters opt in by flipping `live_state.backend` to
    # `"tfc"` and providing the matching credential.
    live_state: LiveStateConfig = Field(default_factory=lambda: LiveStateConfig())


class TfcCredentials(_Strict):
    """Terraform Cloud / HCP / Terraform Enterprise API token.

    A team or user API token with read access to the configured
    organisation's workspaces. The overlay only ever issues GET
    requests; no `manage` / `write` scopes are required.
    """

    token: str


class TerrakubeCredentials(_Strict):
    """Terrakube Personal Access Token.

    A PAT with read access to the configured organisation's workspaces.
    The overlay only ever issues GET requests; no write scopes are
    required."""

    token: str


# Public surface of this module: the shared domain models + `AppConfig`,
# plus every config/credential symbol re-exported from the subsystem
# packages above (so star-imports and `from ...models import X` resolve).
__all__ = [
    "AnthropicCredentials",
    "AppConfig",
    "AzureOpenAICredentials",
    "BedrockConfig",
    "BedrockNarrative",
    "BitbucketCredentials",
    "ConfluenceConfig",
    "ConfluenceCredentials",
    "DiscordCredentials",
    "DiscordNotificationConfig",
    "DiscoveryConfig",
    "EmailCredentials",
    "EmailNotificationConfig",
    "GitHubWikiConfig",
    "GiteaCredentials",
    "GithubCredentials",
    "GitlabCredentials",
    "GraphConfig",
    "HtmlConfig",
    "JsonConfig",
    "LLMConfig",
    "LiveStateConfig",
    "LiveStateInfo",
    "MarkdownConfig",
    "ModuleRef",
    "NotificationConfig",
    "NotionConfig",
    "NotionCredentials",
    "OpenAICredentials",
    "OpsgenieCredentials",
    "OpsgenieNotificationConfig",
    "OutputRef",
    "PagerDutyCredentials",
    "PagerDutyNotificationConfig",
    "ProviderRef",
    "PublisherConfig",
    "RepoInventory",
    "RepoMetadata",
    "ResourceExplanation",
    "ResourceRef",
    "RunOutcome",
    "SecretsConfig",
    "SlackConfig",
    "SlackCredentials",
    "SlackNotificationConfig",
    "SlackWebhookCredentials",
    "SlackWebhookNotificationConfig",
    "SnsNotificationConfig",
    "StaleApplyAlert",
    "StalenessConfig",
    "StateBackend",
    "StateBackendSignal",
    "StdoutNotificationConfig",
    "TeamsCredentials",
    "TeamsNotificationConfig",
    "TerraformSummary",
    "TerrakubeCredentials",
    "TfcCredentials",
    "VariableRef",
    "WebhookCredentials",
    "WebhookNotificationConfig",
    "WorkspaceMappingRule",
]
