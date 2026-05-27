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

    from iac_cartographer.models import AppConfig

logger = logging.getLogger(__name__)

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


# ── Top-level orchestrator ───────────────────────────────────────────


def run_diagnose(config_path: str) -> DiagnoseReport:
    """Run every probe in order and return the aggregate report.

    Order is deliberate: environment checks first (cheap + independent of
    config), then config load (which gates everything else), then per-
    component checks. A failed config load returns early — the per-
    component checks have nothing to probe against."""
    report = DiagnoseReport()

    # 1. Environment — independent of config.
    report.checks.append(_run("terraform-docs", check_terraform_docs))

    # 2. Config load — gates everything below.
    config_result, config = check_config_loads(config_path)
    report.checks.append(config_result)
    if config is None:
        return report

    # 3-7. Per-component config sanity (no live API calls — cheap + offline).
    report.checks.append(_run("optional-deps", lambda: check_optional_deps(config)))
    report.checks.append(_run("discovery", lambda: check_discovery(config)))
    report.checks.append(_run("llm", lambda: check_llm(config)))
    report.checks.append(_run("publisher", lambda: check_publisher_target(config)))
    report.checks.append(_run("notifications", lambda: check_notifications(config)))

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
