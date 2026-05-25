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
from iac_cartographer.aws import get_secret, get_ssm_parameter, put_metric_data
from iac_cartographer.confluence import ConfluenceClient
from iac_cartographer.constants import CartographerError, ConfigError, MissingSecretError
from iac_cartographer.discovery import discover
from iac_cartographer.extractor import run_terraform_docs
from iac_cartographer.fetcher import cleanup, clone
from iac_cartographer.llm import AnthropicBackend, BedrockBackend, LLMBackend
from iac_cartographer.models import (
    AnthropicCredentials,
    AppConfig,
    ConfluenceCredentials,
    GithubCredentials,
    GitlabCredentials,
    LLMConfig,
    RepoInventory,
    RepoMetadata,
    RunOutcome,
    SlackCredentials,
)
from iac_cartographer.narrator import detect_suspicious_phrases, placeholder_narrative, summarize
from iac_cartographer.publishers import ConfluencePublisher, LocalMarkdownPublisher, Publisher
from iac_cartographer.renderer import OVERVIEW_TITLE, compute_sha
from iac_cartographer.slack import SlackNotifier

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


# Default Secrets Manager paths. Conventional, not magical — override
# constants here (or fork) if your org organises secrets differently.
# The `noqa: S105` markers are for bandit which would otherwise flag these
# as hardcoded password strings (they're path identifiers, not values).
CONFLUENCE_SECRET_NAME = "iac-cartographer/confluence"  # noqa: S105
GITLAB_SECRET_NAME = "iac-cartographer/gitlab"  # noqa: S105
GITHUB_SECRET_NAME = "iac-cartographer/github"  # noqa: S105
SLACK_SECRET_NAME = "iac-cartographer/slack"  # noqa: S105
ANTHROPIC_SECRET_NAME = "iac-cartographer/anthropic"  # noqa: S105


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


def _load_secrets(llm_backend_name: str = "bedrock") -> LoadedSecrets:
    """Fetch the Secrets Manager entries the run needs and validate them.

    `llm_backend_name` decides whether the Anthropic API key is required:
    for the default `bedrock` backend it's skipped entirely; for the
    `anthropic` backend it's loaded from `iac-cartographer/anthropic`.

    Raises `MissingSecretError` if any required secret is missing or
    fails Pydantic validation. Always reads real credentials —
    `--dry-run` only suppresses *writes*, never reads.
    """
    try:
        confluence_raw = get_secret(CONFLUENCE_SECRET_NAME)
        gitlab_raw = get_secret(GITLAB_SECRET_NAME)
        github_raw = get_secret(GITHUB_SECRET_NAME)
        slack_raw = get_secret(SLACK_SECRET_NAME)
    except Exception as exc:
        raise MissingSecretError(f"failed to fetch a required secret: {exc}") from exc

    anthropic_creds: AnthropicCredentials | None = None
    if llm_backend_name == "anthropic":
        try:
            anthropic_raw = get_secret(ANTHROPIC_SECRET_NAME)
        except Exception as exc:
            raise MissingSecretError(
                f"llm.backend=anthropic but the {ANTHROPIC_SECRET_NAME} secret is missing: {exc}"
            ) from exc
        try:
            anthropic_creds = AnthropicCredentials.model_validate(anthropic_raw)
        except Exception as exc:
            raise MissingSecretError(f"anthropic secret payload failed schema validation: {exc}") from exc

    try:
        return LoadedSecrets(
            confluence=ConfluenceCredentials.model_validate(confluence_raw),
            gitlab=GitlabCredentials.model_validate(gitlab_raw),
            github=GithubCredentials.model_validate(github_raw),
            slack=SlackCredentials.model_validate(slack_raw),
            anthropic=anthropic_creds,
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
    raise ConfigError(f"unknown publisher.kind: {kind!r}")


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
) -> tuple[RepoInventory | None, str | None, int, int]:
    """Clone → extract → narrate one repo. Returns (inventory, error, tokens_in, tokens_out).

    `error` is non-None when the per-repo pipeline failed; the orchestrator
    records it but doesn't abort. Token counts come from the LLM backend's
    usage data (may be 0 when narration was skipped via `--no-bedrock` or
    when the backend doesn't supply usage counts).
    """
    path: Path | None = None
    try:
        path = await asyncio.to_thread(clone, meta, gitlab_token, github_token)
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
    secrets = _load_secrets(config.llm.backend)
    slack = SlackNotifier(secrets.slack, channel=config.slack.channel)
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
                parent_id = get_ssm_parameter(config.confluence.parent_page_id_ssm_path)
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
                await slack.error(
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
                await slack.error(f"iac-cartographer: preflight failed — {type(exc).__name__}: {exc}")
                return 2

        # ── Discovery ────────────────────────────────────────────────────
        try:
            repos = await discover(config.discovery, secrets.gitlab, secrets.github)
        except CartographerError as exc:
            logger.exception("discovery failed")
            if not args.dry_run:
                await slack.error(f"iac-cartographer: discovery failed — {exc}")
            return 2

        repos = _filter_repos(repos, args.repos)
        outcome = outcome.model_copy(update={"discovered": len(repos)})
        if not repos:
            logger.error("no repos to process after filtering")
            if not args.dry_run:
                await slack.error("iac-cartographer: no repos to process after filtering")
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
                await slack.error(
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
            if suspicious_repos:
                # Append AI-H1 review-queue notice to the message
                review_lines = [f"{repo} → {', '.join(phrases)}" for repo, phrases in suspicious_repos.items()]
                slack_msg += "\n:warning: Narrative review needed (AI-H1 — possible prompt injection): " + "; ".join(
                    review_lines
                )
            if all_failures or suspicious_repos:
                await slack.warn(slack_msg)
            else:
                await slack.info(slack_msg)

        return 1 if all_failures else 0
    finally:
        await slack.close()


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
    parser.add_argument("--verbose", action="store_true", help="DEBUG-level logging.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="One full pipeline run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(verbose=args.verbose)
    try:
        if args.once:
            return run_once(args)
        return 0  # pragma: no cover — argparse `required=True` prevents this branch
    except CartographerError as exc:
        logger.exception("run aborted: %s", exc)
        return 2
    except Exception:
        logger.exception("unhandled exception")
        return 3
