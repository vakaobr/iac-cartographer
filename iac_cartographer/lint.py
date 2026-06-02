"""`iac-cartographer --lint <path>` — single-repo linter for IaC hygiene.

Runs the same `terraform-docs` + HCL-parser pipeline the publish path
uses, but instead of producing a published page it applies a small
set of fix-it rules and emits findings. Different mode from the
scheduled-publish path:

  * No discovery, no clone, no LLM, no publisher, no notifier.
  * Operates on a single local directory the operator already has
    on disk (a CI checkout, a working tree, a pre-commit-hook
    invocation).
  * Exits non-zero when findings exceed the configured threshold,
    so it's CI-gating-friendly.

Rules shipped (v1):

  * **provider-undeclared** (error) — a provider is referenced in
    `resource "..."` blocks but is missing from
    `terraform { required_providers { ... } }`. Terraform's own
    `init` already fails on this for any non-Hashicorp namespace;
    surfacing it as a lint rule catches it before `init` runs.

  * **provider-unpinned** (warn) — declared provider has no
    `version = "..."` constraint. Each `init` may then pick a
    different version → silent drift.

  * **module-unpinned** (warn) — declared module has no `version`
    field. For registry modules this is high-risk (the same `source`
    can resolve to any tag); for git-source modules with a ref
    (`?ref=v1.2.3`) the lint passes via the version parser
    extracting the ref.

  * **no-terraform-files** (info) — the directory contains no
    `.tf` files at all. Not a problem in itself (some operators
    point the linter at a parent dir + walk discovery picks the
    right children), but the CLI surfaces it so a misconfigured
    pre-commit hook fails loud rather than silently passing.

Output formats:

  * **text** (default) — Markdown-ish, one finding per line, colour-
    free so log capture stays grep-friendly.
  * **json** — structured payload for downstream tooling (CI
    dashboards, custom gates, the `iac-cartographer` test harness).
  * **github** — GitHub Actions annotations
    (`::warning file=...,line=...::message`) so a pre-commit hook
    running inside Actions surfaces findings as PR review
    comments.

Exit codes (matches the existing CLI convention):

  * 0 — no findings (or only `info`)
  * 1 — warnings present AND `--fail-on=warn`
  * 2 — errors present (regardless of `--fail-on`)
  * 3 — unhandled exception (propagated from the main entrypoint)
"""

from __future__ import annotations

import json as json_module
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from iac_cartographer.extractor import run_terraform_docs

if TYPE_CHECKING:
    from iac_cartographer.models import TerraformSummary

logger = logging.getLogger("iac_cartographer.lint")


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class LintFinding:
    """One lint finding. Immutable so callers can safely pass lists
    around without worrying about accidental mutation."""

    rule_id: str
    severity: Severity
    message: str
    # `path` is the directory or file the finding relates to (relative
    # to the linter's input root). Empty when the finding applies to
    # the whole repo (e.g. `no-terraform-files`).
    path: str = ""


@dataclass(frozen=True)
class LintReport:
    """Aggregate of every finding from one lint pass + a counts header."""

    findings: list[LintFinding]
    repo_path: str

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == Severity.WARN]

    @property
    def infos(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == Severity.INFO]


# ─── rule engine ──────────────────────────────────────────────────────


def run_lint(path: Path | str) -> LintReport:
    """Run every rule against `path` and return a `LintReport`.

    `path` must be a local directory. The function calls
    `run_terraform_docs(path)` to get a `TerraformSummary`, then
    applies rules to that summary. No network, no clone, no LLM —
    safe to run from a pre-commit hook or a sandboxed CI runner.
    """
    repo_path = Path(path)
    summary = run_terraform_docs(repo_path)
    findings = list(_apply_rules(summary))
    findings.sort(key=_finding_sort_key)
    return LintReport(findings=findings, repo_path=str(repo_path))


def _apply_rules(summary: TerraformSummary) -> list[LintFinding]:
    """Apply every shipped rule. Order matters for output stability —
    keep error-rules first, then warns, then infos."""
    findings: list[LintFinding] = []
    findings.extend(_rule_provider_undeclared(summary))
    findings.extend(_rule_provider_unpinned(summary))
    findings.extend(_rule_module_unpinned(summary))
    findings.extend(_rule_no_terraform_files(summary))
    return findings


def _rule_provider_undeclared(summary: TerraformSummary) -> list[LintFinding]:
    """Providers referenced via resources but never declared in
    `terraform { required_providers { ... } }`. The extractor sets
    `source = None` for these; the renderer would emit
    `(not declared)` markers."""
    return [
        LintFinding(
            rule_id="provider-undeclared",
            severity=Severity.ERROR,
            message=(
                f"provider {provider.name!r} is used but missing from "
                f"`terraform {{ required_providers {{ ... }} }}`. "
                "Terraform init will fail for any non-hashicorp namespace; "
                "add an explicit declaration."
            ),
        )
        for provider in summary.providers
        if provider.source is None
    ]


def _rule_provider_unpinned(summary: TerraformSummary) -> list[LintFinding]:
    """Declared providers without a `version = "..."` constraint.
    Skips providers with `source is None` — those are caught by
    `provider-undeclared` already; double-firing would be noise."""
    return [
        LintFinding(
            rule_id="provider-unpinned",
            severity=Severity.WARN,
            message=(
                f"provider {provider.name!r} ({provider.source}) has no "
                "version constraint — each `terraform init` may resolve a "
                'different version. Pin with `version = "~> X.Y"`.'
            ),
        )
        for provider in summary.providers
        if provider.source is not None and provider.version is None
    ]


def _rule_module_unpinned(summary: TerraformSummary) -> list[LintFinding]:
    """Declared modules without a `version` field."""
    return [
        LintFinding(
            rule_id="module-unpinned",
            severity=Severity.WARN,
            message=(
                f"module {module.name!r} ({module.source}) has no "
                "version pin — registry sources will resolve to the "
                'latest tag at each run. Pin with `version = "X.Y.Z"` '
                "(registry) or `?ref=vX.Y.Z` (git-source)."
            ),
        )
        for module in summary.modules
        if module.version is None
    ]


def _rule_no_terraform_files(summary: TerraformSummary) -> list[LintFinding]:
    """The directory contained no `.tf` files at all. Not an error in
    itself (operator may have aimed the linter wrong), just an
    info-level signal so a misconfigured hook doesn't silently
    succeed."""
    if not summary.providers and not summary.modules and not summary.resources:
        return [
            LintFinding(
                rule_id="no-terraform-files",
                severity=Severity.INFO,
                message=(
                    "no Terraform structural facts extracted. The directory may "
                    "contain no `.tf` files, or they may live in subdirectories "
                    "the extractor's skip-list excludes (`.git/`, `.terraform/`, "
                    "`vendor/`, etc.)."
                ),
            )
        ]
    return []


def _finding_sort_key(f: LintFinding) -> tuple[int, str, str]:
    """Stable sort: errors first, then warns, then infos. Within a
    severity, by rule_id then message — keeps the output deterministic
    across runs."""
    severity_rank = {Severity.ERROR: 0, Severity.WARN: 1, Severity.INFO: 2}[f.severity]
    return severity_rank, f.rule_id, f.message


# ─── exit code policy ─────────────────────────────────────────────────


def compute_exit_code(report: LintReport, fail_on: Severity) -> int:
    """Map a `LintReport` + threshold to a CLI exit code.

    * `fail_on = error` (default): exit 2 on any error, 0 otherwise.
    * `fail_on = warn`: exit 2 on any error, 1 on any warn (no error),
      0 otherwise.
    * `fail_on = info`: exit 2 on any error, 1 on any warn or info.
    """
    if report.errors:
        return 2
    if fail_on == Severity.WARN and report.warnings:
        return 1
    if fail_on == Severity.INFO and (report.warnings or report.infos):
        return 1
    return 0


# ─── renderers ────────────────────────────────────────────────────────


def render_text(report: LintReport) -> str:
    """Human-friendly Markdown. One finding per line, colour-free,
    prefixed with the severity in upper-case so `grep -E '^ERROR'`
    works for CI log filters."""
    if not report.findings:
        return f"## iac-cartographer lint\n\nNo findings for {report.repo_path}.\n"

    lines: list[str] = [
        f"## iac-cartographer lint — {report.repo_path}",
        "",
        f"**{len(report.errors)} error(s), {len(report.warnings)} warning(s), {len(report.infos)} info**",
        "",
    ]
    for finding in report.findings:
        prefix = finding.severity.value.upper()
        location = f" ({finding.path})" if finding.path else ""
        lines.append(f"{prefix}  [{finding.rule_id}]{location}: {finding.message}")
    lines.append("")
    return "\n".join(lines)


def render_json(report: LintReport) -> str:
    """Machine-readable JSON. Stable schema for CI dashboards / custom
    gates / the iac-cartographer test harness:

        {
          "schema": "iac-cartographer.lint.v1",
          "repo_path": "./infra",
          "summary": {"error": 1, "warn": 2, "info": 0},
          "findings": [
            {"rule_id": "...", "severity": "error", "path": "...", "message": "..."},
            ...
          ]
        }
    """
    payload = {
        "schema": "iac-cartographer.lint.v1",
        "repo_path": report.repo_path,
        "summary": {
            "error": len(report.errors),
            "warn": len(report.warnings),
            "info": len(report.infos),
        },
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "path": f.path,
                "message": f.message,
            }
            for f in report.findings
        ],
    }
    return json_module.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def render_github(report: LintReport) -> str:
    """GitHub Actions annotation format. One finding per line.

    `::warning file=path::message` becomes a PR review comment when
    the linter runs inside a GitHub Actions workflow. The annotation
    syntax doesn't have a `info` level — we map info → notice. Error
    and warn map cleanly.

    Reference: https://docs.github.com/en/actions/reference/workflow-commands-for-github-actions
    """
    if not report.findings:
        return ""
    level_map = {
        Severity.ERROR: "error",
        Severity.WARN: "warning",
        Severity.INFO: "notice",
    }
    lines: list[str] = []
    for finding in report.findings:
        gh_level = level_map[finding.severity]
        # `file` is required for the annotation to render in the right
        # place in the PR diff. When the finding's path is empty
        # (repo-wide), point at the repo root so the annotation
        # surfaces somewhere visible.
        file_path = finding.path or report.repo_path
        # Escape the GitHub Actions annotation special chars in the
        # message to avoid breaking the parser on `::`, `,`, `\n`, `\r`.
        safe_message = finding.message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace(":", "%3A")
        lines.append(f"::{gh_level} file={file_path},title={finding.rule_id}::{safe_message}")
    return "\n".join(lines) + "\n"


# Public-facing renderer dispatch — callers pass a format literal.


def render(report: LintReport, fmt: str) -> str:
    """Dispatch to the right renderer. `fmt` is one of
    `"text"` / `"json"` / `"github"` (case-insensitive)."""
    fmt_lower = fmt.lower()
    if fmt_lower == "text":
        return render_text(report)
    if fmt_lower == "json":
        return render_json(report)
    if fmt_lower == "github":
        return render_github(report)
    raise ValueError(f"unknown lint format: {fmt!r} (expected one of: text, json, github)")


__all__ = [
    "LintFinding",
    "LintReport",
    "Severity",
    "compute_exit_code",
    "render",
    "render_github",
    "render_json",
    "render_text",
    "run_lint",
]
