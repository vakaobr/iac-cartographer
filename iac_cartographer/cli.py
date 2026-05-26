"""CLI entrypoint — wires the pipeline phases into a runnable command.

Single mode: `iac-cartographer --once` runs the full discovery → extract → narrate
→ render → publish pipeline once and exits. Designed to be invoked by a scheduler
(EventBridge Scheduler, Kubernetes CronJob, GitHub Actions schedule, plain cron,
…) against the iac-cartographer container or installed Python package.

Flags:
  * `--dry-run`     — load + discover + extract + narrate, but do NOT PUT to
                      Confluence and do NOT send Slack messages.
  * `--no-bedrock`  — use a placeholder narrative instead of invoking Bedrock
                      (debug; saves cost during repeated local iteration).
  * `--repos a,b,c` — restrict the run to a comma-separated list of repo
                      `full_name`s (used for partial reruns).
  * `--config`      — config source (`ssm://…` URI or filesystem path).
  * `--verbose`     — DEBUG-level logging.

JSON-formatted logging to stdout — one line per record — so CloudWatch Logs
Insights queries can grep on it.

Exit codes:
  0  success (every discovered repo published or correctly skipped-unchanged)
  1  partial success (some repos failed; Confluence partially updated; Slack-warned)
  2  known error caught at top level (ConfigError, MissingSecretError, etc.)
  3  unhandled exception
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from iac_cartographer import __version__
from iac_cartographer.aws import get_ssm_parameter, put_metric_data
from iac_cartographer.confluence import ConfluenceClient
from iac_cartographer.constants import CartographerError, ConfigError, MissingSecretError
from iac_cartographer.diff import (
    compute_diff,
    load_prior_inventories,
    render_diff_markdown,
    render_diff_summary,
)
from iac_cartographer.discovery import (
    BitbucketDiscovery,
    DiscoverySource,
    FileDiscovery,
    GiteaDiscovery,
    GithubDiscovery,
    GitlabDiscovery,
    discover_from_sources,
)
from iac_cartographer.extractor import run_terraform_docs
from iac_cartographer.fetcher import cleanup, clone
from iac_cartographer.init_scaffold import (
    InitError,
    print_next_steps,
    write_scaffold,
)
from iac_cartographer.llm import (
    AnthropicBackend,
    AzureOpenAIBackend,
    BedrockBackend,
    LLMBackend,
    OllamaBackend,
    OpenAIBackend,
    VertexBackend,
)
from iac_cartographer.models import (
    AnthropicCredentials,
    AppConfig,
    AzureOpenAICredentials,
    BitbucketCredentials,
    ConfluenceCredentials,
    DiscordCredentials,
    EmailCredentials,
    GiteaCredentials,
    GithubCredentials,
    GitlabCredentials,
    LLMConfig,
    NotionCredentials,
    OpenAICredentials,
    OpsgenieCredentials,
    PagerDutyCredentials,
    RepoInventory,
    RepoMetadata,
    RunOutcome,
    SlackCredentials,
    SlackWebhookCredentials,
    TeamsCredentials,
    WebhookCredentials,
)
from iac_cartographer.narrator import detect_suspicious_phrases, placeholder_narrative, summarize
from iac_cartographer.notifications import (
    NotificationDispatcher,
    NotificationSecrets,
    build_dispatcher,
)
from iac_cartographer.publishers import (
    ConfluencePublisher,
    LocalHtmlPublisher,
    LocalJsonPublisher,
    LocalMarkdownPublisher,
    NotionPublisher,
    Publisher,
)
from iac_cartographer.renderer import OVERVIEW_TITLE, compute_sha
from iac_cartographer.secrets import SecretsProvider, build_provider

logger = logging.getLogger("iac_cartographer.cli")

# Default config source. Production runs typically read this from SSM
# Parameter Store; for local dev / non-AWS deployments pass a filesystem
# path via `--config`. The SSM path is conventional, not magical — override
# in deployment if you organise SSM differently.
DEFAULT_CONFIG_SOURCE = "ssm:///iac-cartographer/config"
_SSM_PREFIX = "ssm://"


# ---------------------------------------------------------------------------
# JSON log formatter — one line per record
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record — easy to query in CloudWatch Logs
    Insights, ELK, Loki, or any structured-log backend."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Token-redaction filter — scrubs anything that looks like a secret out of log
# payloads. Applied as a logging.Filter so it catches both `logger.info("...")`
# and `logger.info("...", extra={"some_dict": ...})`.
# ---------------------------------------------------------------------------

# Match `"api_token": "…"`, `'token': '…'`, `password=…` etc. inside a string
# that may have been produced by repr() on a dict / dataclass. Replacement
# leaves the key visible (useful for debugging which field was scrubbed) but
# masks the value entirely.
_SECRET_KEY_RE = re.compile(
    r"""(['"]?(?:token|api[_-]?token|api[_-]?key|password|secret|bot[_-]?token)['"]?\s*[:=]\s*)(['"])([^'"]+)(['"])""",
    re.IGNORECASE,
)


class _RedactSecretsFilter(logging.Filter):
    """Mask anything matching a known secret-key pattern inside the formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg = _SECRET_KEY_RE.sub(r"\1\2***REDACTED***\4", msg)
        record.args = ()
        return True


def _setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RedactSecretsFilter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


# ---------------------------------------------------------------------------
# Config + secrets loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedSecrets:
    """Bundle of Secrets Manager entries loaded at startup.

    `anthropic` is only populated when `llm.backend == "anthropic"` —
    Bedrock deployments don't need an API key. Everything else is
    required on every run.

    Frozen so downstream phases can't mutate credentials by accident.
    """

    confluence: ConfluenceCredentials
    gitlab: GitlabCredentials
    github: GithubCredentials
    slack: SlackCredentials
    anthropic: AnthropicCredentials | None = None
    # `bitbucket` is only populated when `discovery.bitbucket_workspaces`
    # is non-empty — runs without Bitbucket discovery don't need the secret.
    bitbucket: BitbucketCredentials | None = None
    # `gitea` is only populated when `discovery.gitea_orgs` is non-empty.
    # Same token powers the listing API (discovery) and the clone path
    # (fetcher's `_authed_clone_url`).
    gitea: GiteaCredentials | None = None
    # `azure_openai` is only populated when `llm.backend == "azure_openai"`
    # AND `llm.azure_openai_use_aad` is false. AAD-authenticated
    # deployments skip the secret entirely.
    azure_openai: AzureOpenAICredentials | None = None
    # `openai` is only populated when `llm.backend == "openai"`.
    openai: OpenAICredentials | None = None
    # Webhook-family notification credentials — only populated when the
    # matching `kind:` appears in `config.notifications`. None means the
    # operator hasn't opted into that channel.
    webhook: WebhookCredentials | None = None
    slack_webhook: SlackWebhookCredentials | None = None
    teams: TeamsCredentials | None = None
    # `email` is only populated when any `notifications[].kind == "email"`.
    # SMTP username + password from the `iac-cartographer/email` secret.
    email: EmailCredentials | None = None
    # Pager-escalation credentials — only loaded when the matching
    # `notifications[].kind` is present.
    pagerduty: PagerDutyCredentials | None = None
    opsgenie: OpsgenieCredentials | None = None
    # `discord` is only populated when any `notifications[].kind == "discord"`.
    # The webhook URL doubles as the credential (token embedded in path).
    discord: DiscordCredentials | None = None
    # `notion` is only populated when `publisher.kind == "notion"`.
    notion: NotionCredentials | None = None


# Default Secrets Manager paths. Conventional, not magical — override
# constants here (or fork) if your org organises secrets differently.
# The `noqa: S105` markers are for bandit which would otherwise flag these
# as hardcoded password strings (they're path identifiers, not values).
CONFLUENCE_SECRET_NAME = "iac-cartographer/confluence"  # noqa: S105
GITLAB_SECRET_NAME = "iac-cartographer/gitlab"  # noqa: S105
GITHUB_SECRET_NAME = "iac-cartographer/github"  # noqa: S105
SLACK_SECRET_NAME = "iac-cartographer/slack"  # noqa: S105
ANTHROPIC_SECRET_NAME = "iac-cartographer/anthropic"  # noqa: S105
AZURE_OPENAI_SECRET_NAME = "iac-cartographer/azure_openai"  # noqa: S105
OPENAI_SECRET_NAME = "iac-cartographer/openai"  # noqa: S105
BITBUCKET_SECRET_NAME = "iac-cartographer/bitbucket"  # noqa: S105
# Gitea / Forgejo discovery + clone token. Loaded only when
# `discovery.gitea_orgs` is non-empty.
GITEA_SECRET_NAME = "iac-cartographer/gitea"  # noqa: S105
# Webhook-family notification secrets. Each one is `{"url": "..."}` —
# the URL itself is the credential (URL-embedded SAS token for Teams,
# URL-embedded webhook secret for Slack-incoming / RocketChat / Mattermost,
# operator's choice for the generic webhook).
WEBHOOK_SECRET_NAME = "iac-cartographer/webhook"  # noqa: S105
SLACK_WEBHOOK_SECRET_NAME = "iac-cartographer/slack_webhook"  # noqa: S105
TEAMS_SECRET_NAME = "iac-cartographer/teams"  # noqa: S105
# Email channel SMTP credentials: `{"username": "...", "password": "..."}`.
# No `sns` secret — SNS auth comes from the AWS credential chain.
EMAIL_SECRET_NAME = "iac-cartographer/email"  # noqa: S105
# Pager-escalation channel credentials. PagerDuty's routing key is a
# per-Service integration key; Opsgenie's API key is a per-team
# integration key (region-bound — see OpsgenieChannel).
PAGERDUTY_SECRET_NAME = "iac-cartographer/pagerduty"  # noqa: S105
OPSGENIE_SECRET_NAME = "iac-cartographer/opsgenie"  # noqa: S105
# Discord webhook URL secret. No `stdout` secret — stdout has no
# credential, it just writes to a process stream.
DISCORD_SECRET_NAME = "iac-cartographer/discord"  # noqa: S105
# Notion publisher integration token. Only loaded when
# `publisher.kind == "notion"`. The token is an operator-visible secret
# (visible in the Notion integration UI) — store it via Secrets
# Manager / env var / Vault like every other credential.
NOTION_SECRET_NAME = "iac-cartographer/notion"  # noqa: S105


def _load_config(config_source: str) -> AppConfig:
    """Load + validate the runtime config from either SSM or a file path.

    `config_source` is either:
      * "ssm:///path/to/parameter" — fetched from Systems Manager Parameter
        Store on every call (no cross-run caching). Production default.
      * Any other string — treated as a filesystem path. Used for local dev.

    Raises `ConfigError` if the YAML parses but doesn't match the schema, or
    if a local file is missing.
    """
    if config_source.startswith(_SSM_PREFIX):
        param_name = config_source[len(_SSM_PREFIX) :]
        raw_yaml = get_ssm_parameter(param_name)
    else:
        path = Path(config_source)
        if not path.exists():
            raise ConfigError(f"config file not found: {config_source}")
        raw_yaml = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw_yaml) or {}
    try:
        return AppConfig.model_validate(parsed)
    except Exception as exc:  # ValidationError + anything yaml leaks
        raise ConfigError(f"config validation failed: {exc}") from exc


def _load_secrets(
    provider: SecretsProvider,
    llm_backend_name: str = "bedrock",
    *,
    need_bitbucket: bool = False,
    need_gitea: bool = False,
    need_azure_openai: bool = False,
    need_webhook: bool = False,
    need_slack_webhook: bool = False,
    need_teams: bool = False,
    need_email: bool = False,
    need_pagerduty: bool = False,
    need_opsgenie: bool = False,
    need_discord: bool = False,
    need_notion: bool = False,
) -> LoadedSecrets:
    """Fetch credential bundles via `provider` and validate each one.

    `llm_backend_name` decides whether the Anthropic API key is required:
    for the default `bedrock` backend it's skipped entirely; for the
    `anthropic` backend it's loaded from `iac-cartographer/anthropic`.

    `need_bitbucket` decides whether the Bitbucket credential is required:
    True when `discovery.bitbucket_workspaces` is non-empty.

    `need_azure_openai` decides whether the Azure OpenAI API key is
    required: True when `llm.backend == "azure_openai"` AND
    `llm.azure_openai_use_aad` is false. AAD-authenticated deployments
    skip the secret entirely.

    Raises `MissingSecretError` if any required secret is missing or
    fails Pydantic validation. Always reads real credentials —
    `--dry-run` only suppresses *writes*, never reads.
    """
    try:
        confluence_raw = provider.get_secret(CONFLUENCE_SECRET_NAME)
        gitlab_raw = provider.get_secret(GITLAB_SECRET_NAME)
        github_raw = provider.get_secret(GITHUB_SECRET_NAME)
        slack_raw = provider.get_secret(SLACK_SECRET_NAME)
    except Exception as exc:
        raise MissingSecretError(f"failed to fetch a required secret via {provider.name}: {exc}") from exc

    anthropic_creds: AnthropicCredentials | None = None
    if llm_backend_name == "anthropic":
        try:
            anthropic_raw = provider.get_secret(ANTHROPIC_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"llm.backend=anthropic but the {ANTHROPIC_SECRET_NAME} secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            anthropic_creds = AnthropicCredentials.model_validate(anthropic_raw)
        except Exception as exc:
            raise MissingSecretError(f"anthropic secret payload failed schema validation: {exc}") from exc

    openai_creds: OpenAICredentials | None = None
    if llm_backend_name == "openai":
        try:
            openai_raw = provider.get_secret(OPENAI_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"llm.backend=openai but the {OPENAI_SECRET_NAME} secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            openai_creds = OpenAICredentials.model_validate(openai_raw)
        except Exception as exc:
            raise MissingSecretError(f"openai secret payload failed schema validation: {exc}") from exc

    azure_openai_creds: AzureOpenAICredentials | None = None
    if need_azure_openai:
        try:
            azure_openai_raw = provider.get_secret(AZURE_OPENAI_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"llm.backend=azure_openai (without use_aad) but the "
                f"{AZURE_OPENAI_SECRET_NAME} secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            azure_openai_creds = AzureOpenAICredentials.model_validate(azure_openai_raw)
        except Exception as exc:
            raise MissingSecretError(f"azure_openai secret payload failed schema validation: {exc}") from exc

    bitbucket_creds: BitbucketCredentials | None = None
    if need_bitbucket:
        try:
            bitbucket_raw = provider.get_secret(BITBUCKET_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"discovery.bitbucket_workspaces is set but the {BITBUCKET_SECRET_NAME} "
                f"secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            bitbucket_creds = BitbucketCredentials.model_validate(bitbucket_raw)
        except Exception as exc:
            raise MissingSecretError(f"bitbucket secret payload failed schema validation: {exc}") from exc

    gitea_creds: GiteaCredentials | None = None
    if need_gitea:
        try:
            gitea_raw = provider.get_secret(GITEA_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"discovery.gitea_orgs is set but the {GITEA_SECRET_NAME} "
                f"secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            gitea_creds = GiteaCredentials.model_validate(gitea_raw)
        except Exception as exc:
            raise MissingSecretError(f"gitea secret payload failed schema validation: {exc}") from exc

    webhook_creds: WebhookCredentials | None = None
    if need_webhook:
        try:
            webhook_raw = provider.get_secret(WEBHOOK_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"notifications[].kind=webhook but the {WEBHOOK_SECRET_NAME} "
                f"secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            webhook_creds = WebhookCredentials.model_validate(webhook_raw)
        except Exception as exc:
            raise MissingSecretError(f"webhook secret payload failed schema validation: {exc}") from exc

    slack_webhook_creds: SlackWebhookCredentials | None = None
    if need_slack_webhook:
        try:
            slack_webhook_raw = provider.get_secret(SLACK_WEBHOOK_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"notifications[].kind=slack_webhook but the {SLACK_WEBHOOK_SECRET_NAME} "
                f"secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            slack_webhook_creds = SlackWebhookCredentials.model_validate(slack_webhook_raw)
        except Exception as exc:
            raise MissingSecretError(f"slack_webhook secret payload failed schema validation: {exc}") from exc

    teams_creds: TeamsCredentials | None = None
    if need_teams:
        try:
            teams_raw = provider.get_secret(TEAMS_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"notifications[].kind=teams but the {TEAMS_SECRET_NAME} secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            teams_creds = TeamsCredentials.model_validate(teams_raw)
        except Exception as exc:
            raise MissingSecretError(f"teams secret payload failed schema validation: {exc}") from exc

    email_creds: EmailCredentials | None = None
    if need_email:
        try:
            email_raw = provider.get_secret(EMAIL_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"notifications[].kind=email but the {EMAIL_SECRET_NAME} secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            email_creds = EmailCredentials.model_validate(email_raw)
        except Exception as exc:
            raise MissingSecretError(f"email secret payload failed schema validation: {exc}") from exc

    pagerduty_creds: PagerDutyCredentials | None = None
    if need_pagerduty:
        try:
            pagerduty_raw = provider.get_secret(PAGERDUTY_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"notifications[].kind=pagerduty but the {PAGERDUTY_SECRET_NAME} secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            pagerduty_creds = PagerDutyCredentials.model_validate(pagerduty_raw)
        except Exception as exc:
            raise MissingSecretError(f"pagerduty secret payload failed schema validation: {exc}") from exc

    opsgenie_creds: OpsgenieCredentials | None = None
    if need_opsgenie:
        try:
            opsgenie_raw = provider.get_secret(OPSGENIE_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"notifications[].kind=opsgenie but the {OPSGENIE_SECRET_NAME} secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            opsgenie_creds = OpsgenieCredentials.model_validate(opsgenie_raw)
        except Exception as exc:
            raise MissingSecretError(f"opsgenie secret payload failed schema validation: {exc}") from exc

    discord_creds: DiscordCredentials | None = None
    if need_discord:
        try:
            discord_raw = provider.get_secret(DISCORD_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"notifications[].kind=discord but the {DISCORD_SECRET_NAME} secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            discord_creds = DiscordCredentials.model_validate(discord_raw)
        except Exception as exc:
            raise MissingSecretError(f"discord secret payload failed schema validation: {exc}") from exc

    notion_creds: NotionCredentials | None = None
    if need_notion:
        try:
            notion_raw = provider.get_secret(NOTION_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"publisher.kind=notion but the {NOTION_SECRET_NAME} secret is missing (via {provider.name}): {exc}"
            ) from exc
        try:
            notion_creds = NotionCredentials.model_validate(notion_raw)
        except Exception as exc:
            raise MissingSecretError(f"notion secret payload failed schema validation: {exc}") from exc

    try:
        return LoadedSecrets(
            confluence=ConfluenceCredentials.model_validate(confluence_raw),
            gitlab=GitlabCredentials.model_validate(gitlab_raw),
            github=GithubCredentials.model_validate(github_raw),
            slack=SlackCredentials.model_validate(slack_raw),
            anthropic=anthropic_creds,
            bitbucket=bitbucket_creds,
            gitea=gitea_creds,
            azure_openai=azure_openai_creds,
            openai=openai_creds,
            webhook=webhook_creds,
            slack_webhook=slack_webhook_creds,
            teams=teams_creds,
            email=email_creds,
            pagerduty=pagerduty_creds,
            opsgenie=opsgenie_creds,
            discord=discord_creds,
            notion=notion_creds,
        )
    except Exception as exc:
        raise MissingSecretError(f"secret payload failed schema validation: {exc}") from exc


def _build_llm_backend(llm_config: LLMConfig, secrets: LoadedSecrets) -> LLMBackend:
    """Instantiate the right `LLMBackend` for `llm_config.backend`.

    Adding a new backend means: extend the `Literal` in `LLMConfig.backend`,
    implement the subclass in `llm.py`, and add a new elif here. Keep the
    decision tree centralised so credentials + region wiring lives in one
    spot."""
    name = llm_config.backend
    if name == "bedrock":
        return BedrockBackend(region=llm_config.bedrock_region)
    if name == "anthropic":
        if secrets.anthropic is None:
            # Shouldn't happen — _load_secrets above gates on the same
            # condition — but guard for clarity / future-refactor safety.
            raise ConfigError(
                "llm.backend=anthropic but no AnthropicCredentials were loaded "
                "(check the iac-cartographer/anthropic secret)"
            )
        return AnthropicBackend(
            api_key=secrets.anthropic.api_key,
            base_url=llm_config.anthropic_base_url,
        )
    if name == "vertex":
        # No secret to load — Vertex AI auth flows through Google
        # Application Default Credentials (workload identity in
        # cluster, ADC for local dev, SA key file for batch jobs).
        # The cli's secrets-loading path stays untouched.
        if not llm_config.vertex_project_id:
            raise ConfigError(
                "llm.backend=vertex but llm.vertex_project_id is empty. "
                "Set it to the GCP project ID hosting your Vertex AI Claude endpoint."
            )
        return VertexBackend(
            project_id=llm_config.vertex_project_id,
            region=llm_config.vertex_region,
        )
    if name == "azure_openai":
        if not llm_config.azure_openai_endpoint:
            raise ConfigError(
                "llm.backend=azure_openai but llm.azure_openai_endpoint is empty. "
                "Set it to your Azure OpenAI resource URL "
                "(e.g. https://my-resource.openai.azure.com/)."
            )
        if not llm_config.azure_openai_deployment:
            raise ConfigError(
                "llm.backend=azure_openai but llm.azure_openai_deployment is empty. "
                "Set it to the deployment name you created in Azure OpenAI Studio."
            )
        if llm_config.azure_openai_use_aad:
            return AzureOpenAIBackend(
                endpoint=llm_config.azure_openai_endpoint,
                deployment=llm_config.azure_openai_deployment,
                use_aad=True,
                api_version=llm_config.azure_openai_api_version,
            )
        if secrets.azure_openai is None:
            # Shouldn't happen — _load_secrets gates on the same
            # condition — but guard for clarity / future-refactor safety.
            raise ConfigError(
                "llm.backend=azure_openai but no AzureOpenAICredentials were loaded. "
                "Either set llm.azure_openai_use_aad: true OR populate "
                "the iac-cartographer/azure_openai secret with an api_key."
            )
        return AzureOpenAIBackend(
            endpoint=llm_config.azure_openai_endpoint,
            deployment=llm_config.azure_openai_deployment,
            api_key=secrets.azure_openai.api_key,
            api_version=llm_config.azure_openai_api_version,
        )
    if name == "openai":
        if secrets.openai is None:
            # Shouldn't happen — _load_secrets gates on the same
            # condition — but guard for clarity / future-refactor safety.
            raise ConfigError(
                "llm.backend=openai but no OpenAICredentials were loaded (check the iac-cartographer/openai secret)"
            )
        return OpenAIBackend(
            api_key=secrets.openai.api_key,
            base_url=llm_config.openai_base_url,
            organization=llm_config.openai_organization,
        )
    if name == "ollama":
        # No secret to load — Ollama is zero-auth by default. Behind a
        # reverse proxy that adds auth, pass headers via
        # `llm.ollama_extra_headers` (e.g. {"Authorization": "Bearer ..."}).
        return OllamaBackend(
            base_url=llm_config.ollama_base_url,
            timeout=llm_config.ollama_timeout_seconds,
            extra_headers=llm_config.ollama_extra_headers,
        )
    raise ConfigError(f"unknown llm.backend: {name!r}")


def _build_publisher(
    config: AppConfig,
    secrets: LoadedSecrets,
    *,
    parent_id: str | None,
) -> Publisher:
    """Instantiate the right `Publisher` for `publisher.kind`.

    `parent_id` is the Confluence parent-page ID resolved by the
    orchestrator's preflight check. Only the Confluence publisher uses
    it; the Markdown publisher ignores it.

    Adding a new publisher means: extend the `Literal` in
    `PublisherConfig.kind`, implement the subclass in `publishers/`, and
    add a new elif here. Centralised so config + credentials wiring
    lives in one spot."""
    kind = config.publisher.kind
    if kind == "confluence":
        if parent_id is None:
            # Should never happen — preflight raises ConfigError before
            # we get here if the parent page can't be resolved — but guard
            # for type-checker happiness and future-refactor safety.
            raise ConfigError("publisher.kind=confluence but parent_id was not resolved at preflight")
        client = ConfluenceClient(config.confluence.site, secrets.confluence)
        return ConfluencePublisher(client, config.confluence, parent_id)
    if kind == "markdown":
        return LocalMarkdownPublisher(output_dir=config.markdown.output_dir)
    if kind == "html":
        return LocalHtmlPublisher(output_dir=config.html.output_dir)
    if kind == "json":
        return LocalJsonPublisher(output_dir=config.json_output.output_dir)
    if kind == "notion":
        if secrets.notion is None:
            raise ConfigError(
                "publisher.kind=notion but no NotionCredentials were loaded (check the iac-cartographer/notion secret)"
            )
        if not config.notion.parent_page_id:
            raise ConfigError("publisher.kind=notion but notion.parent_page_id is empty")
        return NotionPublisher(secrets.notion, parent_page_id=config.notion.parent_page_id)
    raise ConfigError(f"unknown publisher.kind: {kind!r}")


def _build_sources(config: AppConfig, secrets: LoadedSecrets) -> list[DiscoverySource]:
    """Instantiate one `DiscoverySource` per configured backend.

    Every field that's non-empty / non-None contributes a source. The
    orchestrator's dedup + deny-list runs on the merged result, so the
    order here only matters for tie-breaking when the same `full_name`
    appears in multiple sources (first-seen wins).

    Adding a new source: append another `if config.discovery.<field>: ...`
    block here, then implement the source class in `discovery/`.
    """
    sources: list[DiscoverySource] = []
    # VCS-host sources are always instantiated (they no-op on empty input)
    # so the orchestrator gets at least the legacy two-source behaviour
    # when only one host is configured.
    if config.discovery.gitlab_group_ids:
        sources.append(
            GitlabDiscovery(
                secrets.gitlab,
                config.discovery.gitlab_group_ids,
                base_url=config.discovery.gitlab_base_url,
            )
        )
    if config.discovery.github_orgs:
        sources.append(GithubDiscovery(secrets.github, config.discovery.github_orgs))
    if config.discovery.bitbucket_workspaces:
        if secrets.bitbucket is None:
            # Shouldn't happen — _load_secrets gates on the same condition —
            # but guard for type-checker happiness and future-refactor safety.
            raise ConfigError(
                "discovery.bitbucket_workspaces is set but no BitbucketCredentials "
                "were loaded (check the iac-cartographer/bitbucket secret)"
            )
        sources.append(BitbucketDiscovery(secrets.bitbucket, config.discovery.bitbucket_workspaces))
    if config.discovery.gitea_orgs:
        if secrets.gitea is None:
            raise ConfigError(
                "discovery.gitea_orgs is set but no GiteaCredentials were loaded "
                "(check the iac-cartographer/gitea secret)"
            )
        if not config.discovery.gitea_base_url:
            raise ConfigError("discovery.gitea_orgs is set but discovery.gitea_base_url is empty")
        sources.append(
            GiteaDiscovery(
                secrets.gitea,
                config.discovery.gitea_orgs,
                base_url=config.discovery.gitea_base_url,
            )
        )
    if config.discovery.repos_file:
        sources.append(FileDiscovery(config.discovery.repos_file))
    return sources


# ---------------------------------------------------------------------------
# Orchestration helpers
# ---------------------------------------------------------------------------


_BEDROCK_SEMAPHORE_LIMIT = 3  # async concurrency cap for Bedrock invocations

# Security: CWE-770 — stop concatenating HCL once cumulative bytes exceed
# this cap. A pathological multi-GB repo would OOM the container before the
# 30 KB narrator-input cap kicks in. 5 MB headroom is generous; typical
# real-world IaC repos are < 1 MB total HCL.
_HCL_BYTE_BUDGET = 5 * 1024 * 1024  # 5 MB


def _read_repo_content(repo_path: Path) -> tuple[str, str]:
    """Return (readme_text, hcl_concat) for Bedrock narration.

    README is the first `README*` file found at the repo root, capped further
    by `narrator.README_CAP_CHARS` inside `build_request`. `hcl_concat` is the
    concatenation of every `*.tf` file under the repo, sorted by path for
    determinism. Cumulative HCL bytes are capped at `_HCL_BYTE_BUDGET` —
    once exceeded, remaining files are skipped and a warning is logged.
    The tail is discarded; downstream truncation at 30 KB inside `build_request`
    means we lose nothing meaningful.
    """
    readme = ""
    for candidate in ("README.md", "README.MD", "README.rst", "README.txt", "README"):
        p = repo_path / candidate
        if p.exists() and p.is_file():
            try:
                readme = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                readme = ""
            break
    hcl_parts: list[str] = []
    cumulative_bytes = 0
    truncated_at: str | None = None
    for tf_file in sorted(repo_path.rglob("*.tf")):
        # Skip vendored / cached terraform state
        rel = tf_file.relative_to(repo_path)
        if any(part in {".terraform", ".git", "vendor", "node_modules"} for part in rel.parts):
            continue
        try:
            stat_size = tf_file.stat().st_size
        except OSError:
            continue
        if cumulative_bytes + stat_size > _HCL_BYTE_BUDGET:
            truncated_at = str(rel)
            break
        try:
            text = tf_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hcl_parts.append(f"# ─── {rel} ───\n{text}")
        cumulative_bytes += stat_size
    if truncated_at is not None:
        logger.warning(
            "_read_repo_content: HCL byte-budget %d exceeded at %s; tail discarded (read %d bytes across %d files)",
            _HCL_BYTE_BUDGET,
            truncated_at,
            cumulative_bytes,
            len(hcl_parts),
        )
    return readme, "\n\n".join(hcl_parts)


async def _process_repo(
    meta: RepoMetadata,
    gitlab_token: str,
    github_token: str,
    llm_config: LLMConfig,
    llm_backend: LLMBackend,
    *,
    no_bedrock: bool,
    semaphore: asyncio.Semaphore,
    gitea_token: str | None = None,
) -> tuple[RepoInventory | None, str | None, int, int]:
    """Clone → extract → narrate one repo. Returns (inventory, error, tokens_in, tokens_out).

    `error` is non-None when the per-repo pipeline failed; the orchestrator
    records it but doesn't abort. Token counts come from the LLM backend's
    usage data (may be 0 when narration was skipped via `--no-bedrock` or
    when the backend doesn't supply usage counts).
    """
    path: Path | None = None
    try:
        path = await asyncio.to_thread(clone, meta, gitlab_token, github_token, gitea_token)
        summary = await asyncio.to_thread(run_terraform_docs, path)
        readme, hcl_concat = await asyncio.to_thread(_read_repo_content, path)

        if no_bedrock:
            narrative = placeholder_narrative()
            tokens_in = 0
            tokens_out = 0
        else:
            async with semaphore:
                narrative, tokens_in, tokens_out = await asyncio.to_thread(
                    summarize, meta, summary, readme, hcl_concat, llm_config, llm_backend
                )

        return (
            RepoInventory(meta=meta, summary=summary, narrative=narrative),
            None,
            tokens_in,
            tokens_out,
        )
    except CartographerError as exc:
        return None, f"{type(exc).__name__}: {exc}", 0, 0
    except Exception as exc:
        # Catch-all so a single bad repo cannot crash the pipeline.
        logger.exception("unexpected error processing %s", meta.full_name)
        return None, f"unexpected: {exc}", 0, 0
    finally:
        if path is not None:
            cleanup(path)


def _filter_repos(repos: list[RepoMetadata], repos_arg: str | None) -> list[RepoMetadata]:
    if not repos_arg:
        return repos
    allowed = {name.strip() for name in repos_arg.split(",") if name.strip()}
    return [r for r in repos if r.full_name in allowed]


def _format_slack_summary(outcome: RunOutcome) -> str:
    """Compose the one-line Slack message body summarising the run."""
    duration = f"{outcome.duration_seconds:.0f}s"
    base = (
        f"iac-cartographer: {outcome.discovered} discovered, "
        f"{outcome.succeeded} succeeded, "
        f"{outcome.skipped_unchanged} unchanged, "
        f"{len(outcome.failed)} failed "
        f"({duration})"
    )
    if outcome.failed:
        details = ", ".join(f"{repo} ({err[:60]})" for repo, err in list(outcome.failed.items())[:3])
        base += f"\nFailures: {details}"
    return base


# ---------------------------------------------------------------------------
# Run mode — the only entry point
# ---------------------------------------------------------------------------


def run_once(args: argparse.Namespace) -> int:
    """Wrapper for the async pipeline so the CLI stays sync-shaped."""
    return asyncio.run(_run_once_async(args))


async def _run_once_async(args: argparse.Namespace) -> int:
    pipeline_url = os.environ.get("CI_JOB_URL") or os.environ.get("PIPELINE_URL")
    started = time.monotonic()
    logger.info(
        "iac-cartographer v%s starting (dry_run=%s, no_bedrock=%s, repos=%s, model=%s, config=%s)",
        __version__,
        args.dry_run,
        args.no_bedrock,
        args.repos or "(all)",
        args.model or "(default)",
        args.config,
    )

    # Heartbeat metric — emitted before any work so it survives even hard
    # failures downstream. Paired with the `iac-cartographer-no-runs` alarm
    # which fires if RunCount is missing for 10+ days (schedule disabled,
    # task failing to start, EventBridge broken).
    if not args.dry_run:
        put_metric_data("IacCartographer", "RunCount", 1.0)

    config = _load_config(args.config)
    # Per-run model override (e.g. validation runs on Haiku, scheduled runs
    # on Sonnet). When `--model` is omitted, config default applies.
    if args.model:
        config = config.model_copy(update={"llm": config.llm.model_copy(update={"model_id": args.model})})
        logger.info("iac-cartographer: LLM model overridden to %s", args.model)
    secrets_provider = build_provider(config.secrets)
    logger.info("secrets: backend=%s", secrets_provider.name)
    # Scan `notifications:` for which webhook-family secrets need loading.
    # Each `kind` maps to its own secret name (`iac-cartographer/<kind>`);
    # the matching `need_*` flag flips on for any entry of that kind so a
    # missing secret fails loud at startup instead of at first notify().
    notification_kinds = {getattr(entry, "kind", None) for entry in config.notifications}
    secrets = _load_secrets(
        secrets_provider,
        config.llm.backend,
        need_bitbucket=bool(config.discovery.bitbucket_workspaces),
        need_gitea=bool(config.discovery.gitea_orgs),
        # Azure OpenAI's API-key secret is needed only when the backend
        # is active AND `use_aad` is off. AAD deployments authenticate
        # via DefaultAzureCredential and don't need a stored key.
        need_azure_openai=(config.llm.backend == "azure_openai" and not config.llm.azure_openai_use_aad),
        need_webhook="webhook" in notification_kinds,
        need_slack_webhook="slack_webhook" in notification_kinds,
        need_teams="teams" in notification_kinds,
        need_email="email" in notification_kinds,
        # No `need_sns`: SNS uses the AWS credential chain, no secret to load.
        need_pagerduty="pagerduty" in notification_kinds,
        need_opsgenie="opsgenie" in notification_kinds,
        need_discord="discord" in notification_kinds,
        # No `need_stdout`: stdout has no credential to load.
        need_notion=config.publisher.kind == "notion",
    )
    notifier: NotificationDispatcher = build_dispatcher(
        config,
        secrets=NotificationSecrets(
            slack=secrets.slack,
            webhook=secrets.webhook,
            slack_webhook=secrets.slack_webhook,
            teams=secrets.teams,
            email=secrets.email,
            pagerduty=secrets.pagerduty,
            opsgenie=secrets.opsgenie,
            discord=secrets.discord,
        ),
    )
    llm_backend = _build_llm_backend(config.llm, secrets)

    outcome = RunOutcome()
    # Resolved once at preflight and reused at publish time to avoid a
    # second SSM read on the happy path.
    parent_id: str | None = None
    try:
        # ── Preflight: Confluence parent page reachability ──────────────
        # Fail-fast if the SSM-stored page ID doesn't resolve, BEFORE we
        # burn discovery / clone / Bedrock-narration on a run we can't
        # publish. Catches: bad SSM value, deleted/moved parent page,
        # revoked Atlassian token, Confluence outage. Skipped under
        # --dry-run (the publish step itself is skipped there) AND for
        # non-Confluence publishers (`markdown` writes to a local dir so
        # there's no parent page concept).
        if not args.dry_run and config.publisher.kind == "confluence":
            try:
                # Resolve the parent page ID. Direct config value wins over
                # the parameter-store lookup so file-based deployments
                # don't need a parameter store at all.
                if config.confluence.parent_page_id:
                    parent_id = config.confluence.parent_page_id
                    logger.info("preflight: using direct config.confluence.parent_page_id")
                else:
                    parent_id = secrets_provider.get_parameter(config.confluence.parent_page_id_ssm_path)
                confluence_preflight = ConfluenceClient(config.confluence.site, secrets.confluence)
                async with confluence_preflight.session() as preflight_session:
                    parent_page = await confluence_preflight.get_page(preflight_session, parent_id)
                logger.info(
                    "preflight: confluence parent page %s reachable (title=%r, version=%d)",
                    parent_id,
                    parent_page.title,
                    parent_page.version,
                )
            except CartographerError as exc:
                logger.exception("preflight: confluence parent page unreachable")
                await notifier.error(
                    f"iac-cartographer: preflight failed — Confluence parent page "
                    f"({config.confluence.parent_page_id_ssm_path}) unreachable: {exc}"
                )
                return 2
            except Exception as exc:
                # boto3 SSM error (parameter missing) or any other unhandled
                # I/O failure. We deliberately catch broadly here — the cost
                # of a false-negative preflight (running a doomed pipeline) is
                # higher than the cost of an over-eager fail (operator retries).
                logger.exception("preflight: unexpected error during Confluence preflight")
                await notifier.error(f"iac-cartographer: preflight failed — {type(exc).__name__}: {exc}")
                return 2

        # ── Discovery ────────────────────────────────────────────────────
        try:
            sources = _build_sources(config, secrets)
            repos = await discover_from_sources(sources, config.discovery.deny_repos)
        except CartographerError as exc:
            logger.exception("discovery failed")
            if not args.dry_run:
                await notifier.error(f"iac-cartographer: discovery failed — {exc}")
            return 2

        repos = _filter_repos(repos, args.repos)
        outcome = outcome.model_copy(update={"discovered": len(repos)})
        if not repos:
            logger.error("no repos to process after filtering")
            if not args.dry_run:
                await notifier.error("iac-cartographer: no repos to process after filtering")
            return 2

        # ── Per-repo pipeline ────────────────────────────────────────────
        semaphore = asyncio.Semaphore(_BEDROCK_SEMAPHORE_LIMIT)
        tasks = [
            _process_repo(
                r,
                secrets.gitlab.token,
                secrets.github.token,
                config.llm,
                llm_backend,
                no_bedrock=args.no_bedrock,
                semaphore=semaphore,
                gitea_token=secrets.gitea.token if secrets.gitea else None,
            )
            for r in repos
        ]
        results = await asyncio.gather(*tasks)
        inventories: list[RepoInventory] = []
        failed: dict[str, str] = {}
        suspicious_repos: dict[str, list[str]] = {}
        tokens_in = 0
        tokens_out = 0
        for repo, (inv, err, tin, tout) in zip(repos, results, strict=True):
            tokens_in += tin
            tokens_out += tout
            if inv is None:
                failed[repo.full_name] = err or "unknown error"
                continue
            # AI-H1 hardening: scan the narrative for trigger phrases that
            # suggest indirect prompt injection. On hit, drop the narrative
            # (page still publishes with structural facts) and queue a
            # Slack-warn for operator review.
            if inv.narrative is not None:
                hits = detect_suspicious_phrases(inv.narrative)
                if hits:
                    logger.warning(
                        "narrative review queue: %s contains suspicious phrase(s) %s — dropping narrative",
                        repo.full_name,
                        hits,
                    )
                    suspicious_repos[repo.full_name] = hits
                    inv = inv.model_copy(update={"narrative": None})
            inventories.append(inv)

        if not inventories:
            # Log every per-repo failure individually so the all-failed path is
            # debuggable. Without these lines a "33 repos failed" Slack alert
            # gives the operator nothing — they have to patch this code to see
            # what `git clone` / `terraform-docs` / Bedrock actually returned.
            for repo_name, err_msg in failed.items():
                logger.error("repo failed: %s — %s", repo_name, err_msg)
            logger.error("every repo failed; nothing to publish (%d failures)", len(failed))
            outcome = outcome.model_copy(
                update={
                    "failed": failed,
                    "bedrock_tokens_in": tokens_in,
                    "bedrock_tokens_out": tokens_out,
                    "duration_seconds": time.monotonic() - started,
                }
            )
            if not args.dry_run:
                # Include the first failure verbatim in Slack so the operator
                # can usually skip the CloudWatch trip. Truncated to keep the
                # message readable; full list is in the per-repo ERROR lines.
                sample = next(iter(failed.values()))[:300] if failed else ""
                await notifier.error(
                    f"iac-cartographer: every repo failed ({len(failed)} repos); no pages updated. First error: {sample}"
                )
            return 1

        # ── Publish ──────────────────────────────────────────────────────
        pages_updated: list[str] = []
        skipped_unchanged = 0
        publish_failures: dict[str, str] = {}
        now = datetime.now(UTC)

        if args.dry_run:
            logger.info("dry-run: would have published %d pages — skipping publisher + Slack", len(inventories) + 1)
        else:
            publisher = _build_publisher(config, secrets, parent_id=parent_id)
            async with publisher:
                # Publish children first so the overview can link to them.
                child_page_ids: dict[str, str] = {}
                for inv in inventories:
                    child_sha = compute_sha(inv)
                    try:
                        result = await publisher.publish_child(
                            inv, sha=child_sha, updated_at=now, pipeline_url=pipeline_url
                        )
                        child_page_ids[inv.meta.full_name] = result.page_id
                        if result.action == "unchanged":
                            skipped_unchanged += 1
                        else:
                            pages_updated.append(result.page_id)
                    except CartographerError as exc:
                        publish_failures[inv.meta.full_name] = f"publisher: {exc}"
                        logger.exception("publisher failed for %s", inv.meta.full_name)

                # Overview SHA includes the full inventory list — adding /
                # removing a repo invalidates the overview banner-SHA.
                overview_sha = compute_sha([inv.model_dump(mode="json") for inv in inventories])
                try:
                    overview_result = await publisher.publish_overview(
                        inventories,
                        child_page_ids,
                        sha=overview_sha,
                        updated_at=now,
                        pipeline_url=pipeline_url,
                    )
                    if overview_result.action == "unchanged":
                        skipped_unchanged += 1
                    else:
                        pages_updated.append(overview_result.page_id)
                except CartographerError as exc:
                    publish_failures[OVERVIEW_TITLE] = f"publisher: {exc}"
                    logger.exception("publisher failed for overview page")

        # ── Between-run diff (optional, --diff PREV_OUTPUT) ─────────────
        # Computed after every repo is built but before the outcome
        # summary is emitted, so `diff_summary` can ride on the
        # end-of-run Slack post alongside the per-run counts.
        diff_summary: str | None = None
        if args.diff:
            prior = load_prior_inventories(args.diff)
            diff = compute_diff(prior, inventories)
            # Markdown to stdout — operators tailing the container log
            # get the full picture; CI artefacts can capture stdout.
            # Use sys.stdout directly (not the logger) so the Markdown
            # stays as-is rather than getting wrapped in a JSON log
            # envelope.
            sys.stdout.write(render_diff_markdown(diff))
            sys.stdout.flush()
            diff_summary = render_diff_summary(diff)

        # ── Outcome + Slack notification ────────────────────────────────
        all_failures = {**failed, **publish_failures}
        outcome = RunOutcome(
            discovered=len(repos),
            succeeded=len(inventories) - len(publish_failures),
            skipped_unchanged=skipped_unchanged,
            failed=all_failures,
            pages_updated=pages_updated,
            duration_seconds=time.monotonic() - started,
            bedrock_tokens_in=tokens_in,
            bedrock_tokens_out=tokens_out,
        )

        # CloudWatch metrics — best effort
        put_metric_data("IacCartographer", "BedrockTokensIn", tokens_in)
        put_metric_data("IacCartographer", "BedrockTokensOut", tokens_out)
        put_metric_data("IacCartographer", "PagesUpdated", float(len(pages_updated)))
        put_metric_data("IacCartographer", "Failures", float(len(all_failures)))
        # AI-H1: surface narrative-review-queue hits as a CloudWatch metric so
        # alarms can fire on any suspected indirect prompt injection.
        put_metric_data("IacCartographer", "SuspiciousNarratives", float(len(suspicious_repos)))

        logger.info(
            "run complete: %s",
            outcome.model_dump_json(exclude={"failed"}),
        )
        if all_failures:
            logger.warning("run failures: %s", all_failures)
        if suspicious_repos:
            logger.warning("suspicious narratives (AI-H1): %s", suspicious_repos)

        if not args.dry_run:
            slack_msg = _format_slack_summary(outcome)
            if diff_summary is not None:
                # `diff_summary` is a one-liner ("3 new, 1 archived, …");
                # appending it to the standard outcome line gives
                # Slack readers the between-run delta without flooding
                # the channel with the full Markdown breakdown (which
                # is on stdout for operators tailing logs).
                slack_msg += f"\n_Diff vs prior run:_ {diff_summary}"
            if suspicious_repos:
                # Append AI-H1 review-queue notice to the message
                review_lines = [f"{repo} → {', '.join(phrases)}" for repo, phrases in suspicious_repos.items()]
                slack_msg += "\n:warning: Narrative review needed (AI-H1 — possible prompt injection): " + "; ".join(
                    review_lines
                )
            if all_failures or suspicious_repos:
                await notifier.warn(slack_msg)
            else:
                await notifier.info(slack_msg)

        return 1 if all_failures else 0
    finally:
        await notifier.close()


# ---------------------------------------------------------------------------
# argparse + entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iac-cartographer",
        description=(
            "Fleet-level documentation for your Terraform/IaC estate. "
            "Discovers IaC repos across GitLab + GitHub, parses with terraform-docs, "
            "narrates with a Claude model on AWS Bedrock, publishes to Confluence."
        ),
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_SOURCE,
        help=(
            "Config source. Either an `ssm://<parameter-name>` URI (production "
            "default — reads SSM Parameter Store) or a filesystem path to a "
            "config.yaml (used for local dev / dry-run testing)."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip Confluence PUT + Slack send.")
    parser.add_argument(
        "--no-bedrock",
        action="store_true",
        help="Use a placeholder narrative instead of invoking Bedrock (debug).",
    )
    parser.add_argument(
        "--repos",
        default=None,
        help="Comma-separated list of repo full_names to restrict the run to.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Bedrock model ID / inference-profile to use for narration. "
            "Defaults to whatever `bedrock.model_id` says in the config "
            "(typically a Sonnet variant for production runs). Use a Haiku "
            "inference-profile ID for cheap validation runs."
        ),
    )
    parser.add_argument(
        "--diff",
        default=None,
        metavar="PREV_OUTPUT",
        help=(
            "Compute a between-run change summary against the prior JSON-publisher output. "
            "PREV_OUTPUT is the directory the previous run wrote its JSON output to "
            "(`json.output_dir` from that run's config — typically `./iac-inventory-json/`). "
            "The diff is printed to stdout as Markdown and attached to the end-of-run notification. "
            "First-run shape: pass a path that doesn't yet exist and every repo shows as `added`. "
            "Independent of `publisher.kind` — this run's publisher can be anything."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG-level logging.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="One full pipeline run.")
    mode.add_argument(
        "--init",
        action="store_true",
        help=(
            "First-time scaffolder. Writes a starter config.yaml (and an "
            "optional .env template for the `env` secrets backend) and prints "
            "next-steps guidance. Combine with --secrets-backend, --publisher, "
            "--llm, --config-path, --env-path, --force."
        ),
    )

    # --init-specific flags. They're top-level so argparse can validate them
    # eagerly; the dispatcher ignores them when running `--once`.
    init_group = parser.add_argument_group(
        "init scaffolder options (only meaningful with --init)",
    )
    init_group.add_argument(
        "--secrets-backend",
        choices=["aws", "env", "vault"],
        default="env",
        help="Secrets backend to scaffold (default: env).",
    )
    init_group.add_argument(
        "--publisher",
        choices=["confluence", "markdown"],
        default="markdown",
        help="Publisher backend to scaffold (default: markdown).",
    )
    init_group.add_argument(
        "--llm",
        choices=["bedrock", "anthropic"],
        default="anthropic",
        help="LLM backend to scaffold (default: anthropic).",
    )
    init_group.add_argument(
        "--config-path",
        default="./iac-cartographer.config.yaml",
        help="Where to write the generated config (default: ./iac-cartographer.config.yaml).",
    )
    init_group.add_argument(
        "--env-path",
        default="./iac-cartographer.env",
        help=(
            "Where to write the .env template (default: ./iac-cartographer.env). Only used when --secrets-backend=env."
        ),
    )
    init_group.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files at the target paths.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(verbose=args.verbose)
    try:
        if args.once:
            return run_once(args)
        if args.init:
            return _run_init(args)
        return 0  # pragma: no cover — argparse `required=True` prevents this branch
    except CartographerError as exc:
        logger.exception("run aborted: %s", exc)
        return 2
    except Exception:
        logger.exception("unhandled exception")
        return 3


def _run_init(args: argparse.Namespace) -> int:
    """Dispatcher for `iac-cartographer --init`. Returns exit code."""
    config_path = Path(args.config_path)
    # The .env template is only relevant for the env secrets backend; pass
    # None to write_scaffold for the other backends so it skips the write.
    env_path = Path(args.env_path) if args.secrets_backend == "env" else None
    try:
        written = write_scaffold(
            config_path=config_path,
            env_path=env_path,
            secrets_backend=args.secrets_backend,
            publisher_kind=args.publisher,
            llm_backend=args.llm,
            force=args.force,
        )
    except InitError as exc:
        logger.error("init: %s", exc)
        return 2
    print_next_steps(
        written,
        secrets_backend=args.secrets_backend,
        publisher_kind=args.publisher,
    )
    return 0
