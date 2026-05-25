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

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    """Base for every model in this module — rejects unknown fields.

    The renderer must not silently drop new terraform-docs fields; the CLI
    must not silently ignore typos in `config.yaml`. Strict-by-default makes
    those failures loud.
    """

    model_config = ConfigDict(extra="forbid")


# ─── Discovery ─────────────────────────────────────────────────────────────


class RepoMetadata(_Strict):
    host: Literal["gitlab", "github"]
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
    # Glob patterns (against full_name) to exclude from publishing — e.g.
    # `*-archived`, `examples/*`, `vendor-*`.
    deny_repos: list[str] = Field(default_factory=list)
    # Optional override for the owning-team guess: full_name → team string.
    # Useful when team mapping isn't trivially derivable from the repo path.
    owner_overrides: dict[str, str] = Field(default_factory=dict)
    # Self-hosted GitLab base URL (without `/api/v4` suffix). Override to point
    # at gitlab.example.com; defaults to gitlab.com.
    gitlab_base_url: str = "https://gitlab.com"


class BedrockConfig(_Strict):
    # Default to a Claude Sonnet model in the EU cross-region inference profile.
    # Override via SSM/YAML to flip between Sonnet (production) and Haiku (cheap
    # validation runs) without a rebuild. IAM allow-list controls which model
    # IDs are actually invocable.
    model_id: str = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
    max_tokens: int = 4096
    # Increments when the system prompt changes — invalidates banner-SHA history
    # so all pages get a forced republish on the next run.
    system_prompt_version: str = "v1"


class ConfluenceConfig(_Strict):
    # Atlassian Cloud site without protocol or trailing slash.
    # Example: "your-org.atlassian.net". The placeholder is obviously invalid
    # in production — Confluence requests will fail loudly with DNS errors
    # if left unset — but it keeps the model validatable for tests/dry-runs.
    site: str = "your-org.atlassian.net"
    # Confluence space key (e.g. "DOCS", "Engineering"). The parent page must
    # already exist in this space; iac-cartographer publishes child pages under it.
    space_key: str = "DOCS"
    # SSM Parameter Store path holding the parent page's numeric ID as a plain
    # string. The parent page is the overview; child pages live under it.
    parent_page_id_ssm_path: str = "/iac-cartographer/confluence-parent-id"


class SlackConfig(_Strict):
    # `#channel-name` or a Slack channel ID (`C0...`). The bot token must be
    # invited to this channel.
    channel: str = "#alerts"


class AppConfig(_Strict):
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    bedrock: BedrockConfig = Field(default_factory=BedrockConfig)
    confluence: ConfluenceConfig = Field(default_factory=ConfluenceConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)


# ─── Secrets (one model per Secrets Manager entry) ─────────────────────────


class ConfluenceCredentials(_Strict):
    email: str
    api_token: str


class GitlabCredentials(_Strict):
    token: str


class GithubCredentials(_Strict):
    token: str


class SlackCredentials(_Strict):
    bot_token: str
    # The channel always comes from `SlackConfig.channel` (SSM-backed); this
    # field is retained for back-compat with older secret payloads but is no
    # longer required. The SlackNotifier ignores it when the orchestrator
    # passes `channel=config.slack.channel` (it always does).
    channel_id: str | None = None
