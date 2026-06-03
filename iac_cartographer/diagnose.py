"""`iac-cartographer --diagnose` — pre-flight self-test of the active config.

Runs a series of probes against the environment + the loaded config and
reports a per-component checklist on stderr. Designed to be the first
command a new adopter runs after `iac-cartographer --init`, and the first
command an existing adopter runs when a scheduled run misbehaves.

The probes are intentionally narrow: each check is one concrete failure
mode an adopter has actually hit (or could plausibly hit on first run).
The point is to turn "the run failed somewhere, grep CloudWatch" into
"--diagnose says the Vault path is wrong, fix that one thing."

Each probe is wrapped in `_run()` which converts any exception into a
`FAIL` CheckResult — probes never propagate. Tests inject pre-built
config / secrets objects to exercise each branch in isolation, so the
module is fully testable offline.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from iac_cartographer.constants import CartographerError

if TYPE_CHECKING:
    from collections.abc import Callable

    from iac_cartographer.cli import LoadedSecrets
    from iac_cartographer.models import AppConfig

logger = logging.getLogger(__name__)

# Live probes touch the network; keep every one short so `--diagnose --live`
# stays interactive even when a backend is hung. A few seconds is plenty for
# a metadata/list call against a healthy endpoint and fails fast against a
# dead one.
_LIVE_HTTP_TIMEOUT_S = 5.0

# Per-million-token pricing for the `--probe-llm` cost line. List prices in
# USD as of 2026-06; we don't track regional / negotiated / cached rates
# (the probe is one call, not a budget). Entries are matched against
# `config.llm.model_id` via `startswith` so versioned model IDs
# (`claude-sonnet-4-6@20260415`, `eu.anthropic.claude-sonnet-4-6-...`)
# resolve to the family price. Unknown models fall back to "≈ negligible"
# rather than guess — operators see token counts either way.
_LLM_PRICE_PER_MTOKEN_USD: dict[str, tuple[float, float]] = {
    # Anthropic Claude
    "claude-sonnet-4-6": (3.00, 15.00),
    "eu.anthropic.claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "eu.anthropic.claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "eu.anthropic.claude-haiku-4-5": (1.00, 5.00),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
}


def _estimate_probe_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> str:
    """Return a `≈ $0.000023` string for the cost line, or an empty
    string when the model isn't in the lookup table. Inputs and outputs
    are priced separately because Anthropic's output rate is ~5x input.
    """
    in_rate: float | None = None
    out_rate: float | None = None
    for prefix, (in_p, out_p) in _LLM_PRICE_PER_MTOKEN_USD.items():
        if model_id.startswith(prefix):
            in_rate, out_rate = in_p, out_p
            break
    if in_rate is None or out_rate is None:
        return ""
    usd = (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
    # 1-token probes round to fractions of a cent; show 6 decimals so the
    # operator sees the actual figure (`$0.000033`) rather than `$0.00`.
    return f"≈ ${usd:.6f}"


# Pinned to match the Dockerfile + ci.yml. A different `terraform-docs`
# version isn't fatal (the JSON output schema is stable across 0.20.x and
# 0.24.x in our usage), but it's the kind of skew that produces
# hard-to-debug "works on my machine" reports.
PINNED_TERRAFORM_DOCS_VERSION = "v0.20.0"
_TF_DOCS_TIMEOUT_S = 10
_TF_DOCS_VERSION_RE = re.compile(r"v(\d+\.\d+\.\d+)")


class Status(StrEnum):
    """Per-check verdict. Renders as a single colour-able glyph on stderr."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    """One probe's verdict.

    `hint` is shown only on non-OK results and should be a single actionable
    sentence ("run `pip install iac-cartographer[notion]`", not
    "consider checking your environment").
    """

    name: str
    status: Status
    detail: str
    hint: str | None = None


@dataclass
class DiagnoseReport:
    """Aggregate result of a `--diagnose` run. The CLI dispatcher
    renders this to stderr and uses `exit_code` as its return code."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 = all OK, 1 = warnings present, 2 = at least one failure.

        SKIP doesn't degrade the exit code (an unconfigured channel
        legitimately has nothing to check; that's not a failure)."""
        if any(c.status == Status.FAIL for c in self.checks):
            return 2
        if any(c.status == Status.WARN for c in self.checks):
            return 1
        return 0

    @property
    def counts(self) -> dict[Status, int]:
        out = dict.fromkeys(Status, 0)
        for c in self.checks:
            out[c.status] += 1
        return out


# ── Probe helpers ────────────────────────────────────────────────────


def _run(name: str, probe: Callable[[], CheckResult]) -> CheckResult:
    """Invoke `probe` and convert any exception into a FAIL result.

    Probes are expected to return a CheckResult; raising is treated as an
    unexpected internal error (still reported as a fail so the operator
    sees something specific instead of a stack trace)."""
    try:
        return probe()
    except Exception as exc:  # probe code is allowed to raise; surface as fail
        logger.exception("diagnose: probe %s raised", name)
        return CheckResult(name=name, status=Status.FAIL, detail=f"probe raised: {exc}")


def _run2(
    name: str,
    probe: Callable[[], tuple[CheckResult, LoadedSecrets | None]],
) -> tuple[CheckResult, LoadedSecrets | None]:
    """`_run` for probes that also return a payload (the loaded secrets).

    Any exception becomes a FAIL with a None payload, so a raising secrets
    probe degrades gracefully into "skip everything downstream"."""
    try:
        return probe()
    except Exception as exc:
        logger.exception("diagnose: live probe %s raised", name)
        return CheckResult(name=name, status=Status.FAIL, detail=f"probe raised: {exc}"), None


# ── Individual probes ────────────────────────────────────────────────


def check_terraform_docs() -> CheckResult:
    """`terraform-docs` is on PATH and at the pinned version."""
    binary = shutil.which("terraform-docs")
    if binary is None:
        return CheckResult(
            name="terraform-docs",
            status=Status.FAIL,
            detail="not found on PATH",
            hint="brew install terraform-docs  # or download from https://terraform-docs.io",
        )
    try:
        result = subprocess.run(  # noqa: S603 — `terraform-docs` resolved via shutil.which above
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=_TF_DOCS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="terraform-docs",
            status=Status.FAIL,
            detail="`terraform-docs --version` timed out",
            hint="binary may be corrupted; reinstall",
        )
    output = (result.stdout + result.stderr).strip()
    match = _TF_DOCS_VERSION_RE.search(output)
    if not match:
        return CheckResult(
            name="terraform-docs",
            status=Status.WARN,
            detail=f"version unparseable from `{output[:80]}`",
        )
    version = f"v{match.group(1)}"
    if version != PINNED_TERRAFORM_DOCS_VERSION:
        return CheckResult(
            name="terraform-docs",
            status=Status.WARN,
            detail=f"{version} installed; project pins {PINNED_TERRAFORM_DOCS_VERSION}",
            hint=(
                f"reinstall {PINNED_TERRAFORM_DOCS_VERSION} for output-shape parity, "
                "or ignore if you've validated your version against the extractor"
            ),
        )
    return CheckResult(name="terraform-docs", status=Status.OK, detail=version)


# Mapping from a config selector (LLM backend / publisher kind / channel kind)
# to the import name that the optional dep installs. The key is whatever
# string appears in `llm.backend`, `publisher.kind`, or
# `notifications[].kind`; the value is `(module_to_import, extras_group)`.
# `extras_group` is what an operator would type after `pip install
# iac-cartographer[...]`.
#
# Backends that ship in the base install (boto3 for bedrock, anthropic SDK
# already pinned, httpx for ollama) intentionally have no entry here.
_OPTIONAL_DEP_MAP: dict[str, tuple[str, str]] = {
    "vertex": ("anthropic.types.beta.messages", "gcp"),
    "azure_openai": ("openai", "azure"),
    "openai": ("openai", "openai"),
    "notion": ("notion_client", "notion"),
    "email": ("aiosmtplib", "email"),
}


def check_optional_deps(config: AppConfig) -> CheckResult:
    """Every backend the active config references has its extras installed."""
    needed: list[tuple[str, str]] = []  # (selector, extras_group)
    if config.llm.backend in _OPTIONAL_DEP_MAP:
        needed.append((config.llm.backend, _OPTIONAL_DEP_MAP[config.llm.backend][1]))
    if config.publisher.kind == "notion":
        needed.append(("notion", _OPTIONAL_DEP_MAP["notion"][1]))
    for entry in config.notifications:
        kind = getattr(entry, "kind", None)
        if kind in _OPTIONAL_DEP_MAP:
            needed.append((kind, _OPTIONAL_DEP_MAP[kind][1]))

    if not needed:
        return CheckResult(
            name="optional-deps",
            status=Status.OK,
            detail="config uses base-install backends only",
        )

    missing: list[tuple[str, str]] = []
    for selector, extras_group in needed:
        module_name = _OPTIONAL_DEP_MAP[selector][0]
        if importlib.util.find_spec(module_name) is None:
            missing.append((selector, extras_group))

    if not missing:
        ok_list = ", ".join(sorted({extras_group for _, extras_group in needed}))
        return CheckResult(
            name="optional-deps",
            status=Status.OK,
            detail=f"all extras present ({ok_list})",
        )
    # First missing group becomes the actionable hint; rest are listed in
    # the detail so the operator sees them all up-front.
    groups = sorted({extras_group for _, extras_group in missing})
    first_group = groups[0]
    return CheckResult(
        name="optional-deps",
        status=Status.FAIL,
        detail=f"missing extras for: {', '.join(s for s, _ in missing)}",
        hint=f"pip install 'iac-cartographer[{','.join(groups) if len(groups) > 1 else first_group}]'",
    )


def check_config_loads(config_path: str) -> tuple[CheckResult, AppConfig | None]:
    """Config file (or SSM parameter) parses + validates against AppConfig."""
    from iac_cartographer.cli import _load_config

    try:
        config = _load_config(config_path)
    except CartographerError as exc:
        return (
            CheckResult(
                name="config",
                status=Status.FAIL,
                detail=str(exc)[:200],
                hint="fix the YAML and re-run --diagnose",
            ),
            None,
        )
    return (
        CheckResult(name="config", status=Status.OK, detail=config_path),
        config,
    )


def check_publisher_target(config: AppConfig) -> CheckResult:
    """The configured publisher's write target is reachable / writable.

    Per publisher kind:
      * confluence / notion / github_wiki — we only check that the required
        config fields are populated (not placeholder values like
        `your-org.atlassian.net` / `REPLACE_ME-...`). We deliberately do
        NOT make live API calls here; that requires credentials we haven't
        loaded yet and would slow --diagnose down. The publisher's own
        runtime preflight handles those.
      * markdown / html / json — the output dir is writable (or creatable).
    """
    kind = config.publisher.kind
    if kind == "confluence":
        site = config.confluence.site
        if not site or "your-org" in site or "REPLACE_ME" in site:
            return CheckResult(
                name="publisher",
                status=Status.FAIL,
                detail=f"confluence.site is unconfigured ({site!r})",
                hint="set confluence.site to your Atlassian Cloud site (no protocol)",
            )
        return CheckResult(name="publisher", status=Status.OK, detail=f"confluence → {site}")
    if kind == "notion":
        parent = config.notion.parent_page_id
        if not parent or "REPLACE_ME" in parent:
            return CheckResult(
                name="publisher",
                status=Status.FAIL,
                detail="notion.parent_page_id is unset",
                hint="create a Notion page, share it with your integration, paste the UUID",
            )
        return CheckResult(name="publisher", status=Status.OK, detail=f"notion → {parent[:8]}…")
    if kind == "github_wiki":
        owner = config.github_wiki.owner
        repo = config.github_wiki.repo
        if not owner or not repo:
            return CheckResult(
                name="publisher",
                status=Status.FAIL,
                detail="github_wiki.{owner,repo} is unset",
                hint="set both to the GitHub repo whose wiki you want to publish to",
            )
        return CheckResult(name="publisher", status=Status.OK, detail=f"github_wiki → {owner}/{repo}.wiki")
    if kind in ("markdown", "html", "json"):
        # Use the matching block on AppConfig — `json` is `json_output`
        # on the model to avoid shadowing BaseModel.json().
        block = getattr(config, "json_output" if kind == "json" else kind)
        out = Path(block.output_dir)
        # Walk up to the first existing parent and check it's writable.
        # We don't auto-create here (--diagnose is read-only); just report.
        existing = out
        while existing != existing.parent and not existing.exists():
            existing = existing.parent
        if not os.access(existing, os.W_OK):
            return CheckResult(
                name="publisher",
                status=Status.FAIL,
                detail=f"{kind}.output_dir ({out}) — nearest existing parent {existing} is not writable",
                hint="chmod / chown the directory or pick a writable path",
            )
        return CheckResult(name="publisher", status=Status.OK, detail=f"{kind} → {out}")
    return CheckResult(  # pragma: no cover — Pydantic Literal blocks this branch
        name="publisher",
        status=Status.WARN,
        detail=f"unknown publisher.kind={kind!r}",
    )


def check_discovery(config: AppConfig) -> CheckResult:
    """At least one discovery source is configured.

    The orchestrator (`discover_from_sources`) raises if all five sources
    are empty — but it raises mid-run, after work has started. Catching
    it here saves a wasted scheduled invocation."""
    d = config.discovery
    configured: list[str] = []
    if d.gitlab_group_ids:
        configured.append(f"gitlab ({len(d.gitlab_group_ids)} group(s))")
    if d.github_orgs:
        configured.append(f"github ({len(d.github_orgs)} org(s))")
    if d.bitbucket_workspaces:
        configured.append(f"bitbucket ({len(d.bitbucket_workspaces)} workspace(s))")
    if d.gitea_orgs:
        configured.append(f"gitea ({len(d.gitea_orgs)} org(s))")
    if d.repos_file:
        configured.append(f"file ({d.repos_file})")

    if not configured:
        return CheckResult(
            name="discovery",
            status=Status.FAIL,
            detail="no discovery sources configured",
            hint="set at least one of: gitlab_group_ids, github_orgs, bitbucket_workspaces, gitea_orgs, repos_file",
        )
    # Gitea requires a base URL (every Gitea / Forgejo install is self-hosted).
    if d.gitea_orgs and not d.gitea_base_url:
        return CheckResult(
            name="discovery",
            status=Status.FAIL,
            detail=f"gitea_orgs set ({d.gitea_orgs}) but discovery.gitea_base_url is empty",
            hint="set discovery.gitea_base_url to your Gitea / Forgejo deployment URL",
        )
    return CheckResult(name="discovery", status=Status.OK, detail=" + ".join(configured))


def check_llm(config: AppConfig) -> CheckResult:
    """LLM config is internally consistent (required fields for the
    chosen backend are populated)."""
    backend = config.llm.backend
    if backend == "vertex" and not config.llm.vertex_project_id:
        return CheckResult(
            name="llm",
            status=Status.FAIL,
            detail="llm.backend=vertex but vertex_project_id is unset",
            hint="set llm.vertex_project_id to the GCP project hosting Claude on Vertex",
        )
    if backend == "azure_openai":
        if not config.llm.azure_openai_endpoint:
            return CheckResult(
                name="llm",
                status=Status.FAIL,
                detail="llm.backend=azure_openai but azure_openai_endpoint is unset",
                hint="set llm.azure_openai_endpoint to your resource URL",
            )
        if not config.llm.azure_openai_deployment:
            return CheckResult(
                name="llm",
                status=Status.FAIL,
                detail="llm.backend=azure_openai but azure_openai_deployment is unset",
                hint="set llm.azure_openai_deployment to the deployment NAME from Azure Studio",
            )
    return CheckResult(
        name="llm",
        status=Status.OK,
        detail=f"{backend} → {config.llm.azure_openai_deployment or config.llm.model_id}",
    )


def check_live_state(config: AppConfig) -> CheckResult:
    """Live-state overlay config is internally consistent.

    Default `backend: "none"` is the no-op path — short-circuit with a
    SKIP so the report doesn't grow noisier for adopters who don't use
    the feature. Any other backend hard-requires `organization` to be
    set; `staleness` knobs are reported even when disabled so an operator
    can tell at a glance which thresholds are in effect.
    """
    ls = config.live_state
    if ls.backend == "none":
        return CheckResult(
            name="live-state",
            status=Status.SKIP,
            detail="live_state.backend=none (no overlay configured)",
        )
    if not ls.organization:
        return CheckResult(
            name="live-state",
            status=Status.FAIL,
            detail=f"live_state.backend={ls.backend} but live_state.organization is unset",
            hint="set live_state.organization to the platform organisation name",
        )
    if ls.backend == "terrakube" and (not ls.hostname or ls.hostname == "app.terraform.io"):
        return CheckResult(
            name="live-state",
            status=Status.FAIL,
            detail="live_state.backend=terrakube but live_state.hostname is unset or still the TFC default",
            hint="set live_state.hostname to your Terrakube install (e.g. terrakube.acme.internal)",
        )
    detail = f"{ls.backend} → org={ls.organization} hostname={ls.hostname}"
    if ls.staleness.enabled:
        ack = (
            f", ack-stale={len(ls.staleness.acknowledged_stale)} pattern(s)" if ls.staleness.acknowledged_stale else ""
        )
        detail += f"; staleness threshold={ls.staleness.threshold_days} day(s){ack}"
    else:
        detail += "; staleness disabled"
    return CheckResult(name="live-state", status=Status.OK, detail=detail)


def check_notifications(config: AppConfig) -> CheckResult:
    """Notification routing is sane (at least one channel, no
    contradictory configuration)."""
    # `notifications: []` is legitimately silent (CI / dry-run / air-gapped).
    if not config.notifications:
        return CheckResult(
            name="notifications",
            status=Status.SKIP,
            detail="no notification channels configured (silent dispatcher)",
        )
    kinds = [getattr(entry, "kind", "?") for entry in config.notifications]
    return CheckResult(
        name="notifications",
        status=Status.OK,
        detail=f"{len(kinds)} channel(s): {', '.join(kinds)}",
    )


def check_secrets_required(config: AppConfig) -> list[CheckResult]:
    """Offline scan of which credentials the active config will actually
    pull at startup. One result per subsystem-credential pair, labelled
    `secrets.<name>`:

      * `Status.OK`   — required by the active config; will be loaded.
                         Detail names the subsystem ("required by
                         publisher.kind=confluence").
      * `Status.SKIP` — not loaded by this run (the matching subsystem
                         isn't active). Detail says "not active".
      * `Status.OK`   — `secrets.slack` in legacy `notifications: []`
                         mode is OPTIONAL — loaded if present, silent
                         dispatcher if absent. Detail says
                         "optional (legacy slack path)".

    This is the answer to "why is iac-cartographer asking for X?": the
    operator can read the report and see exactly which subsystem
    triggers each credential, without any live API calls. Complements
    the live `secrets-live` check, which actually fetches.
    """
    notification_kinds = {getattr(entry, "kind", None) for entry in config.notifications}
    publisher_kind = config.publisher.kind
    using_repos_file = bool(config.discovery.repos_file)

    def _row(name: str, *, reason: str | None) -> CheckResult:
        if reason is None:
            return CheckResult(name=f"secrets.{name}", status=Status.SKIP, detail="not active")
        return CheckResult(name=f"secrets.{name}", status=Status.OK, detail=f"required by {reason}")

    out: list[CheckResult] = []

    # Publisher credentials.
    out.append(_row("confluence", reason="publisher.kind=confluence" if publisher_kind == "confluence" else None))
    out.append(_row("notion", reason="publisher.kind=notion" if publisher_kind == "notion" else None))
    # Live-state overlay credentials. Each is loaded only when the
    # matching backend is selected — the other is silently skipped.
    out.append(
        _row(
            "tfc",
            reason=f"live_state.backend={config.live_state.backend}" if config.live_state.backend == "tfc" else None,
        )
    )
    out.append(
        _row(
            "terrakube",
            reason=(
                f"live_state.backend={config.live_state.backend}" if config.live_state.backend == "terrakube" else None
            ),
        )
    )

    # Discovery + Git-host credentials. `repos_file` can list repos on
    # any host, so a curated file forces both VCS tokens (we can't
    # know the hosts without reading the file).
    gitlab_reason = (
        "discovery.gitlab_group_ids"
        if config.discovery.gitlab_group_ids
        else ("discovery.repos_file" if using_repos_file else None)
    )
    out.append(_row("gitlab", reason=gitlab_reason))

    github_triggers: list[str] = []
    if config.discovery.github_orgs:
        github_triggers.append("discovery.github_orgs")
    if publisher_kind == "github_wiki":
        github_triggers.append("publisher.kind=github_wiki")
    if using_repos_file:
        github_triggers.append("discovery.repos_file")
    out.append(_row("github", reason=" + ".join(github_triggers) or None))

    out.append(
        _row("bitbucket", reason="discovery.bitbucket_workspaces" if config.discovery.bitbucket_workspaces else None)
    )
    out.append(_row("gitea", reason="discovery.gitea_orgs" if config.discovery.gitea_orgs else None))

    # LLM credentials.
    llm_backend = config.llm.backend
    out.append(_row("anthropic", reason="llm.backend=anthropic" if llm_backend == "anthropic" else None))
    out.append(_row("openai", reason="llm.backend=openai" if llm_backend == "openai" else None))
    azure_reason = (
        "llm.backend=azure_openai (without use_aad)"
        if llm_backend == "azure_openai" and not config.llm.azure_openai_use_aad
        else None
    )
    out.append(_row("azure_openai", reason=azure_reason))

    # Notification channels. Slack has a third state (legacy optional).
    if "slack" in notification_kinds:
        out.append(_row("slack", reason="notifications[].kind=slack"))
    elif not config.notifications:
        out.append(
            CheckResult(
                name="secrets.slack",
                status=Status.OK,
                detail="optional (legacy `notifications: []` path — loaded if present, silent if absent)",
            )
        )
    else:
        out.append(_row("slack", reason=None))

    out.extend(
        _row(kind, reason=f"notifications[].kind={kind}" if kind in notification_kinds else None)
        for kind in ("webhook", "slack_webhook", "teams", "email", "pagerduty", "opsgenie", "discord")
    )

    return out


# ── Live probes (only with --live) ───────────────────────────────────
#
# Every live probe actually touches the configured backend. They run only
# under `--diagnose --live`, only after the matching offline probe passed
# (no point pinging an LLM whose config is broken), and — like the offline
# probes — they NEVER raise (`_run` converts any escape into a FAIL). Each
# uses a short timeout so a hung backend can't stall the whole report.


def check_secrets_live(config: AppConfig) -> tuple[CheckResult, LoadedSecrets | None]:
    """Build the configured secrets provider and fetch the required bundle.

    This is the highest-value live check: a wrong Vault path / missing
    Secrets Manager entry / empty env var is the single most common reason
    a real run dies at startup. We mirror exactly what `cli._load_secrets`
    resolves for the active config (the same conditional `need_*` set), so
    a green result here means the run's credential-load step will succeed.

    Returns the loaded bundle alongside the verdict so downstream live
    probes (discovery / publisher) can reuse the already-fetched tokens
    instead of hitting the secrets backend a second time.
    """
    from iac_cartographer.cli import _load_secrets
    from iac_cartographer.secrets import build_provider

    try:
        provider = build_provider(config.secrets)
    except CartographerError as exc:
        return (
            CheckResult(
                name="secrets-live",
                status=Status.FAIL,
                detail=f"could not build {config.secrets.backend} provider: {exc}",
                hint="fix the secrets.* block (see secrets.backend-specific fields)",
            ),
            None,
        )

    notification_kinds = {getattr(entry, "kind", None) for entry in config.notifications}
    try:
        secrets = _load_secrets(
            provider,
            config.llm.backend,
            need_confluence=config.publisher.kind == "confluence",
            need_gitlab=bool(config.discovery.gitlab_group_ids) or bool(config.discovery.repos_file),
            need_github=(
                bool(config.discovery.github_orgs)
                or config.publisher.kind == "github_wiki"
                or bool(config.discovery.repos_file)
            ),
            need_slack="slack" in notification_kinds,
            try_slack=not config.notifications,
            need_bitbucket=bool(config.discovery.bitbucket_workspaces),
            need_gitea=bool(config.discovery.gitea_orgs),
            need_azure_openai=(config.llm.backend == "azure_openai" and not config.llm.azure_openai_use_aad),
            need_webhook="webhook" in notification_kinds,
            need_slack_webhook="slack_webhook" in notification_kinds,
            need_teams="teams" in notification_kinds,
            need_email="email" in notification_kinds,
            need_pagerduty="pagerduty" in notification_kinds,
            need_opsgenie="opsgenie" in notification_kinds,
            need_discord="discord" in notification_kinds,
            need_notion=config.publisher.kind == "notion",
            need_tfc=config.live_state.backend == "tfc",
            need_terrakube=config.live_state.backend == "terrakube",
        )
    except CartographerError as exc:
        return (
            CheckResult(
                name="secrets-live",
                status=Status.FAIL,
                detail=str(exc)[:200],
                hint=f"populate the missing/invalid secret in the {provider.name} backend",
            ),
            None,
        )
    return (
        CheckResult(
            name="secrets-live",
            status=Status.OK,
            detail=f"all required secrets resolved via {provider.name}",
        ),
        secrets,
    )


def check_discovery_live(config: AppConfig, secrets: LoadedSecrets) -> CheckResult:
    """One cheap authenticated API call per configured discovery source.

    Confirms the token actually works (not just that it's present). We hit
    each host's cheap `whoami`-style endpoint (`/user`, `/api/v1/user`,
    `/2.0/user`) — never a full repo enumeration.
    """
    import httpx

    from iac_cartographer.discovery import (
        BitbucketDiscovery,
        GiteaDiscovery,
        GithubDiscovery,
        GitlabDiscovery,
    )

    d = config.discovery
    probed: list[str] = []

    def _get(source: object, path: str, params: dict[str, object] | None = None) -> httpx.Response:
        # Reuse the source's auth headers + base URL (both private attrs the
        # discovery clients expose); make one tiny GET with a short timeout.
        with httpx.Client(
            base_url=source._base_url,  # type: ignore[attr-defined]
            headers=source._headers,  # type: ignore[attr-defined]
            timeout=_LIVE_HTTP_TIMEOUT_S,
        ) as client:
            return client.get(path, params=params or {})

    if d.gitlab_group_ids and secrets.gitlab is not None:
        src = GitlabDiscovery(secrets.gitlab, d.gitlab_group_ids, base_url=d.gitlab_base_url)
        resp = _get(src, "/user")
        if resp.status_code >= 400:
            return CheckResult(
                name="discovery-live",
                status=Status.FAIL,
                detail=f"gitlab auth failed (status={resp.status_code})",
                hint="check the iac-cartographer/gitlab token scope (read_api) + base URL",
            )
        probed.append("gitlab")
    if d.github_orgs and secrets.github is not None:
        src = GithubDiscovery(secrets.github, d.github_orgs)
        resp = _get(src, "/user")
        if resp.status_code >= 400:
            return CheckResult(
                name="discovery-live",
                status=Status.FAIL,
                detail=f"github auth failed (status={resp.status_code})",
                hint="check the iac-cartographer/github token (repo + read:org scopes)",
            )
        probed.append("github")
    if d.bitbucket_workspaces and secrets.bitbucket is not None:
        src = BitbucketDiscovery(secrets.bitbucket, d.bitbucket_workspaces)
        resp = _get(src, "/2.0/user")
        if resp.status_code >= 400:
            return CheckResult(
                name="discovery-live",
                status=Status.FAIL,
                detail=f"bitbucket auth failed (status={resp.status_code})",
                hint="check the iac-cartographer/bitbucket token / app-password",
            )
        probed.append("bitbucket")
    if d.gitea_orgs and d.gitea_base_url and secrets.gitea is not None:
        src = GiteaDiscovery(secrets.gitea, d.gitea_orgs, base_url=d.gitea_base_url)
        resp = _get(src, "/api/v1/user")
        if resp.status_code >= 400:
            return CheckResult(
                name="discovery-live",
                status=Status.FAIL,
                detail=f"gitea auth failed (status={resp.status_code})",
                hint="check the iac-cartographer/gitea token + gitea_base_url",
            )
        probed.append("gitea")

    if not probed:
        # Only the file source is configured — nothing to authenticate.
        return CheckResult(
            name="discovery-live",
            status=Status.SKIP,
            detail="no API-backed discovery sources to authenticate (file source only)",
        )
    return CheckResult(
        name="discovery-live",
        status=Status.OK,
        detail=f"authenticated: {', '.join(probed)}",
    )


def check_llm_live(config: AppConfig, secrets: LoadedSecrets, *, probe_llm: bool = False) -> CheckResult:
    """Per-backend reachability probe.

    Default (cost-safe) mode never runs a completion. For Ollama we GET the
    unauthenticated `/api/tags` listing. For every API-key / cloud backend
    we confirm the client constructs with credentials present — a build-only
    check, not an inference call — because a real completion would cost
    tokens on every diagnose. The detail string says which mode was used so
    the operator knows a green result means "constructs + creds present",
    not "a generation succeeded".

    When `probe_llm=True` (opt-in via `--diagnose --live --probe-llm`), we
    additionally issue ONE bounded `max_tokens=1` completion for true end-
    to-end confidence that creds + model + endpoint work for inference, not
    just that the client constructs. This costs a fraction of a cent of real
    spend — hence opt-in. Ollama is exempt (local + free) and keeps the
    /api/tags listing as its primary check.
    """
    import httpx

    from iac_cartographer.cli import _build_llm_backend

    backend = config.llm.backend
    if backend == "ollama":
        with httpx.Client(timeout=_LIVE_HTTP_TIMEOUT_S) as client:
            resp = client.get(f"{config.llm.ollama_base_url.rstrip('/')}/api/tags")
        if resp.status_code >= 400:
            return CheckResult(
                name="llm-live",
                status=Status.FAIL,
                detail=f"ollama /api/tags returned {resp.status_code}",
                hint="is `ollama serve` running at llm.ollama_base_url?",
            )
        try:
            models = resp.json().get("models", [])
        except (ValueError, AttributeError):
            models = []
        return CheckResult(
            name="llm-live",
            status=Status.OK,
            detail=f"ollama reachable ({len(models)} model(s) listed via /api/tags)",
        )

    # Construct the client (validates required config + that any API-key
    # credential was loaded). Bedrock/Vertex resolve credentials lazily via
    # the cloud provider chain, so "constructs" is the strongest cost-safe
    # guarantee we can make without spending tokens.
    llm_backend = _build_llm_backend(config.llm, secrets)

    if not probe_llm:
        return CheckResult(
            name="llm-live",
            status=Status.OK,
            detail=f"{backend} client constructs (no completion run — cost-safe; pass --probe-llm for a real check)",
        )

    # Opt-in real-inference probe: one bounded, single-token completion.
    # Mirrors how the narrator calls invoke() (model_id from config) but
    # with max_tokens=1 + a trivial prompt to keep spend to a fraction of
    # a cent. Any error → FAIL with an actionable hint.
    try:
        response = llm_backend.invoke(
            model_id=config.llm.model_id,
            system_prompt="Reply with the single word: ok",
            user_blocks=[{"type": "text", "text": "ping"}],
            max_tokens=1,
        )
    except Exception as exc:
        return CheckResult(
            name="llm-live",
            status=Status.FAIL,
            detail=f"{backend} 1-token probe failed: {str(exc)[:160]}",
            hint="check the credential's invoke permission, the model_id, and the endpoint/region",
        )
    cost = _estimate_probe_cost_usd(config.llm.model_id, response.input_tokens, response.output_tokens)
    cost_segment = f" {cost}" if cost else " ≈ negligible (model not in price table)"
    return CheckResult(
        name="llm-live",
        status=Status.OK,
        detail=(
            f"{backend} completed a real 1-token probe "
            f"(in={response.input_tokens}, out={response.output_tokens} tokens;"
            f"{cost_segment})"
        ),
    )


def check_live_state_live(config: AppConfig, secrets: LoadedSecrets) -> CheckResult:
    """Probe the configured live-state platform for token + reachability.

    `backend: "none"` short-circuits to SKIP. Other backends hit a cheap
    auth-validating endpoint:

      * `tfc` — `GET /api/v2/account/details` (the standard TFC token
        check; no workspace data fetched).
      * `terrakube` — `GET /api/v1/organization?filter[organization]=name==<org>`
        (resolves the configured organisation to a UUID, doubles as a
        token + connectivity probe).

    Any HTTP / network failure becomes a FAIL with an actionable hint;
    the orchestrator continues without the overlay rather than crashing
    the run (see the equivalent fallback path in `cli.py::_run_once_async`).
    """
    import httpx

    if config.live_state.backend == "none":
        return CheckResult(
            name="live-state-live",
            status=Status.SKIP,
            detail="live_state.backend=none — no live overlay to probe",
        )
    if config.live_state.backend == "tfc":
        if secrets.tfc is None:
            return CheckResult(
                name="live-state-live",
                status=Status.FAIL,
                detail="live_state.backend=tfc but no TfcCredentials were loaded",
                hint='populate the iac-cartographer/tfc secret with `{"token": "..."}`',
            )
        url = f"https://{config.live_state.hostname.rstrip('/')}/api/v2/account/details"
        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"Bearer {secrets.tfc.token}"},
                timeout=_LIVE_HTTP_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            return CheckResult(
                name="live-state-live",
                status=Status.FAIL,
                detail=f"tfc account/details probe failed: {type(exc).__name__}: {str(exc)[:140]}",
                hint=(
                    "confirm live_state.hostname is reachable and the iac-cartographer/tfc token "
                    "is valid (e.g. `curl -H 'Authorization: Bearer …' "
                    f"{url}` should return 200)"
                ),
            )
        if resp.status_code == 401:
            return CheckResult(
                name="live-state-live",
                status=Status.FAIL,
                detail="tfc returned 401 Unauthorized — the API token is invalid or expired",
                hint="rotate iac-cartographer/tfc; the overlay needs a read-scoped team or user token",
            )
        if resp.status_code >= 400:
            return CheckResult(
                name="live-state-live",
                status=Status.FAIL,
                detail=f"tfc account/details returned HTTP {resp.status_code}",
                hint="check live_state.hostname (TFE installs need their own hostname, not app.terraform.io)",
            )
        try:
            attrs = (resp.json().get("data") or {}).get("attributes") or {}
            username = attrs.get("username") or "?"
        except (ValueError, AttributeError):
            username = "?"
        return CheckResult(
            name="live-state-live",
            status=Status.OK,
            detail=f"tfc reachable at {config.live_state.hostname} as {username}",
        )
    if config.live_state.backend == "terrakube":
        if secrets.terrakube is None:
            return CheckResult(
                name="live-state-live",
                status=Status.FAIL,
                detail="live_state.backend=terrakube but no TerrakubeCredentials were loaded",
                hint='populate the iac-cartographer/terrakube secret with `{"token": "..."}`',
            )
        hostname = config.live_state.hostname.rstrip("/")
        url = f"https://{hostname}/api/v1/organization"
        try:
            resp = httpx.get(
                url,
                params={
                    "filter[organization]": f"name=={config.live_state.organization}",
                    "page[limit]": "1",
                },
                headers={
                    "Authorization": f"Bearer {secrets.terrakube.token}",
                    "Accept": "application/vnd.api+json",
                },
                timeout=_LIVE_HTTP_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            return CheckResult(
                name="live-state-live",
                status=Status.FAIL,
                detail=f"terrakube probe failed: {type(exc).__name__}: {str(exc)[:140]}",
                hint=(
                    f"confirm live_state.hostname ({hostname}) is reachable and "
                    "the iac-cartographer/terrakube PAT is valid"
                ),
            )
        if resp.status_code == 401:
            return CheckResult(
                name="live-state-live",
                status=Status.FAIL,
                detail="terrakube returned 401 Unauthorized — the PAT is invalid or expired",
                hint="rotate iac-cartographer/terrakube; the overlay needs a read-scoped PAT",
            )
        if resp.status_code >= 400:
            return CheckResult(
                name="live-state-live",
                status=Status.FAIL,
                detail=f"terrakube /organization returned HTTP {resp.status_code}",
                hint="check live_state.hostname and that the PAT can list organisations",
            )
        try:
            data = resp.json().get("data") or []
        except ValueError:
            data = []
        match = next(
            (
                e
                for e in data
                if isinstance(e, dict) and ((e.get("attributes") or {}).get("name") == config.live_state.organization)
            ),
            None,
        )
        if match is None:
            return CheckResult(
                name="live-state-live",
                status=Status.FAIL,
                detail=(
                    f"terrakube reachable at {hostname} but organisation "
                    f"{config.live_state.organization!r} is not visible to this PAT"
                ),
                hint="confirm the organisation name + that the PAT has access",
            )
        return CheckResult(
            name="live-state-live",
            status=Status.OK,
            detail=(f"terrakube reachable at {hostname}; organisation {config.live_state.organization!r} resolved"),
        )
    return CheckResult(
        name="live-state-live",
        status=Status.FAIL,
        detail=f"unknown live_state.backend: {config.live_state.backend!r}",
        hint="supported backends: `tfc`, `terrakube` (or `none` to disable)",
    )


def check_publisher_live(config: AppConfig, secrets: LoadedSecrets) -> CheckResult:
    """Confirm the publisher's write target is reachable with credentials.

    confluence — GET the parent page. notion — retrieve the parent page.
    github_wiki — `git ls-remote` the wiki repo. markdown/html/json have no
    remote target (the offline probe already checked dir writability), so
    they SKIP here.
    """
    import asyncio

    import httpx

    kind = config.publisher.kind

    if kind == "confluence":
        from iac_cartographer.confluence import ConfluenceClient
        from iac_cartographer.secrets import build_provider

        if secrets.confluence is None:  # pragma: no cover — offline probe + need_confluence gate this
            return CheckResult(
                name="publisher-live",
                status=Status.FAIL,
                detail="no ConfluenceCredentials loaded",
                hint="populate the iac-cartographer/confluence secret",
            )
        parent_id = config.confluence.parent_page_id
        if not parent_id:
            provider = build_provider(config.secrets)
            parent_id = provider.get_parameter(config.confluence.parent_page_id_ref)
        client = ConfluenceClient(config.confluence.site, secrets.confluence)

        async def _probe() -> str:
            async with client.session() as session:
                page = await client.get_page(session, parent_id)
                return page.title

        title = asyncio.run(_probe())
        return CheckResult(
            name="publisher-live",
            status=Status.OK,
            detail=f"confluence parent page reachable (title={title!r})",
        )

    if kind == "notion":
        if secrets.notion is None:  # pragma: no cover — offline probe + need_notion gate this
            return CheckResult(
                name="publisher-live",
                status=Status.FAIL,
                detail="no NotionCredentials loaded",
                hint="populate the iac-cartographer/notion secret",
            )
        page_id = config.notion.parent_page_id
        resp = httpx.get(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers={
                "Authorization": f"Bearer {secrets.notion.integration_token}",
                "Notion-Version": "2022-06-28",
            },
            timeout=_LIVE_HTTP_TIMEOUT_S,
        )
        if resp.status_code >= 400:
            return CheckResult(
                name="publisher-live",
                status=Status.FAIL,
                detail=f"notion parent page retrieve returned {resp.status_code}",
                hint="share the page with your integration + check the iac-cartographer/notion token",
            )
        return CheckResult(
            name="publisher-live",
            status=Status.OK,
            detail=f"notion parent page reachable ({page_id[:8]}…)",
        )

    if kind == "github_wiki":
        if secrets.github is None:  # pragma: no cover — offline probe + need_github gate this
            return CheckResult(
                name="publisher-live",
                status=Status.FAIL,
                detail="no GithubCredentials loaded",
                hint="populate the iac-cartographer/github secret",
            )
        owner = config.github_wiki.owner
        repo = config.github_wiki.repo
        url = f"https://{secrets.github.token}@github.com/{owner}/{repo}.wiki.git"
        result = subprocess.run(  # noqa: S603 — args fully constructed below, no shell
            ["git", "ls-remote", url, "HEAD"],  # noqa: S607 — `git` resolved via PATH; deployment-controlled
            capture_output=True,
            text=True,
            timeout=_LIVE_HTTP_TIMEOUT_S,
            check=False,
        )
        if result.returncode != 0:
            sanitized = result.stderr.replace(secrets.github.token, "<TOKEN>")
            return CheckResult(
                name="publisher-live",
                status=Status.FAIL,
                detail=f"git ls-remote on {owner}/{repo}.wiki failed: {sanitized.strip()[:160]}",
                hint="initialise the wiki (create the first page) + check the github token",
            )
        return CheckResult(
            name="publisher-live",
            status=Status.OK,
            detail=f"github_wiki reachable ({owner}/{repo}.wiki)",
        )

    # markdown / html / json — no remote target; offline probe covered it.
    return CheckResult(
        name="publisher-live",
        status=Status.SKIP,
        detail=f"{kind} has no remote target (dir writability checked offline)",
    )


# ── Top-level orchestrator ───────────────────────────────────────────


def run_diagnose(config_path: str, *, live: bool = False, probe_llm: bool = False) -> DiagnoseReport:
    """Run every probe in order and return the aggregate report.

    Order is deliberate: environment checks first (cheap + independent of
    config), then config load (which gates everything else), then per-
    component checks. A failed config load returns early — the per-
    component checks have nothing to probe against.

    When `live=True`, the offline probes run first as always, then live
    reachability probes run for each component whose offline probe passed
    (a broken config / missing dep means there's nothing live to reach).

    When `probe_llm=True` (only meaningful with `live=True`), the live LLM
    probe additionally issues one bounded `max_tokens=1` completion — real
    spend, hence opt-in.
    """
    report = DiagnoseReport()

    # 1. Environment — independent of config.
    report.checks.append(_run("terraform-docs", check_terraform_docs))

    # 2. Config load — gates everything below.
    config_result, config = check_config_loads(config_path)
    report.checks.append(config_result)
    if config is None:
        return report

    # 3-7. Per-component config sanity (no live API calls — cheap + offline).
    offline: dict[str, CheckResult] = {
        "optional-deps": _run("optional-deps", lambda: check_optional_deps(config)),
        "discovery": _run("discovery", lambda: check_discovery(config)),
        "llm": _run("llm", lambda: check_llm(config)),
        "publisher": _run("publisher", lambda: check_publisher_target(config)),
        "notifications": _run("notifications", lambda: check_notifications(config)),
        "live-state": _run("live-state", lambda: check_live_state(config)),
    }
    report.checks.extend(offline.values())

    # 7b. Offline secret inventory. One row per credential the loader
    # could touch, marking each as required-by-something or not-active.
    # Cheap to compute (pure config inspection — zero API calls) and
    # the highest-signal explanation of "why is iac-cartographer asking
    # for the X secret?" — the answer reads off the report directly.
    report.checks.extend(check_secrets_required(config))

    if not live:
        return report

    # 8. Live reachability — only with --live, only where the offline probe
    # passed. Secrets first (it gates discovery + publisher, which need the
    # loaded tokens). A failed offline probe skips its live counterpart.
    secrets_result, secrets = _run2("secrets-live", lambda: check_secrets_live(config))
    report.checks.append(secrets_result)
    if secrets is None:
        # No credentials → nothing else live can run. Surface explicit skips
        # so the operator sees the chain stopped rather than silently absent.
        for name in ("discovery-live", "llm-live", "publisher-live", "live-state-live"):
            report.checks.append(
                CheckResult(name=name, status=Status.SKIP, detail="skipped — secrets-live did not resolve")
            )
        return report

    if offline["discovery"].status == Status.OK:
        report.checks.append(_run("discovery-live", lambda: check_discovery_live(config, secrets)))
    else:
        report.checks.append(
            CheckResult(name="discovery-live", status=Status.SKIP, detail="skipped — offline discovery probe failed")
        )

    if offline["llm"].status == Status.OK:
        report.checks.append(_run("llm-live", lambda: check_llm_live(config, secrets, probe_llm=probe_llm)))
    else:
        report.checks.append(
            CheckResult(name="llm-live", status=Status.SKIP, detail="skipped — offline llm probe failed")
        )

    if offline["publisher"].status == Status.OK:
        report.checks.append(_run("publisher-live", lambda: check_publisher_live(config, secrets)))
    else:
        report.checks.append(
            CheckResult(name="publisher-live", status=Status.SKIP, detail="skipped — offline publisher probe failed")
        )

    if offline["live-state"].status in (Status.OK, Status.SKIP):
        report.checks.append(_run("live-state-live", lambda: check_live_state_live(config, secrets)))
    else:
        report.checks.append(
            CheckResult(
                name="live-state-live",
                status=Status.SKIP,
                detail="skipped — offline live-state probe failed",
            )
        )

    return report


# ── Rendering ────────────────────────────────────────────────────────

_GLYPH = {
    Status.OK: "✓",
    Status.WARN: "!",
    Status.FAIL: "✗",
    Status.SKIP: "·",
}


def render(report: DiagnoseReport) -> str:
    """Multi-line stderr-friendly checklist + a summary footer."""
    lines: list[str] = [
        "iac-cartographer --diagnose",
        "=" * 35,
        "",
    ]
    name_w = max(len(c.name) for c in report.checks)
    for c in report.checks:
        lines.append(f"{_GLYPH[c.status]} {c.name:<{name_w}}  {c.detail}")
        if c.hint and c.status != Status.OK:
            lines.append(f"  {'':<{name_w}}    → {c.hint}")

    counts = report.counts
    lines.append("")
    summary_bits = []
    if counts[Status.OK]:
        summary_bits.append(f"{counts[Status.OK]} ok")
    if counts[Status.WARN]:
        summary_bits.append(f"{counts[Status.WARN]} warn")
    if counts[Status.FAIL]:
        summary_bits.append(f"{counts[Status.FAIL]} fail")
    if counts[Status.SKIP]:
        summary_bits.append(f"{counts[Status.SKIP]} skip")
    lines.append(", ".join(summary_bits) or "no checks ran")

    if report.exit_code == 0:
        lines.append("All checks passed. Ready to run --once.")
    elif report.exit_code == 1:
        lines.append("Warnings only — runs will likely succeed but worth fixing.")
    else:
        lines.append("At least one check failed. Fix above before running --once.")

    return "\n".join(lines) + "\n"
