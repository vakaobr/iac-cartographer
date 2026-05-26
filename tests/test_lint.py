"""Tests for the `--lint` subcommand.

Coverage:
  * Each rule fires on the right `TerraformSummary` shape (and doesn't
    fire spuriously).
  * Each output format (text / json / github) round-trips a sample
    report into a parseable shape.
  * `compute_exit_code` maps severity + threshold to the right code.
  * The CLI dispatcher honours `--format` and `--fail-on`.
"""

from __future__ import annotations

import json as json_module
from pathlib import Path
from unittest.mock import patch

import pytest

from iac_cartographer.lint import (
    LintFinding,
    LintReport,
    Severity,
    compute_exit_code,
    render,
    render_github,
    render_json,
    render_text,
    run_lint,
)
from iac_cartographer.models import (
    ModuleRef,
    ProviderRef,
    ResourceRef,
    TerraformSummary,
)

# ── Rule tests (via patched run_terraform_docs) ──────────────────────


def _run_with_summary(summary: TerraformSummary, *, path: str = "/fake/repo") -> LintReport:
    """Helper: patch the extractor to return `summary` and run the
    linter. Keeps the rule tests free of filesystem fixtures."""
    with patch("iac_cartographer.lint.run_terraform_docs", return_value=summary):
        return run_lint(path)


def test_undeclared_provider_fires_error() -> None:
    summary = TerraformSummary(
        providers=[ProviderRef(name="cloudflare", source=None, version=None)],
        resources=[ResourceRef(type="cloudflare_zone", name="z")],
    )
    report = _run_with_summary(summary)
    errors = report.errors
    assert len(errors) == 1
    assert errors[0].rule_id == "provider-undeclared"
    assert "cloudflare" in errors[0].message


def test_undeclared_provider_does_not_double_fire_as_unpinned() -> None:
    """A provider that's both undeclared AND has no version should
    only fire `provider-undeclared` — once you fix the declaration,
    the unpinned check applies. Firing both at once is noise."""
    summary = TerraformSummary(
        providers=[ProviderRef(name="cloudflare", source=None, version=None)],
        resources=[ResourceRef(type="cloudflare_zone", name="z")],
    )
    report = _run_with_summary(summary)
    rule_ids = {f.rule_id for f in report.findings}
    assert "provider-undeclared" in rule_ids
    assert "provider-unpinned" not in rule_ids


def test_unpinned_provider_fires_warn() -> None:
    summary = TerraformSummary(
        providers=[ProviderRef(name="aws", source="hashicorp/aws", version=None)],
        resources=[ResourceRef(type="aws_instance", name="vm")],
    )
    report = _run_with_summary(summary)
    warnings = report.warnings
    assert len(warnings) == 1
    assert warnings[0].rule_id == "provider-unpinned"
    assert "aws" in warnings[0].message


def test_pinned_provider_is_silent() -> None:
    summary = TerraformSummary(
        providers=[ProviderRef(name="aws", source="hashicorp/aws", version=">= 5.0")],
        resources=[ResourceRef(type="aws_instance", name="vm")],
    )
    report = _run_with_summary(summary)
    assert [f for f in report.findings if f.severity != Severity.INFO] == []


def test_unpinned_module_fires_warn() -> None:
    summary = TerraformSummary(
        modules=[ModuleRef(name="vpc", source="terraform-aws-modules/vpc/aws", version=None)],
    )
    report = _run_with_summary(summary)
    warnings = [f for f in report.warnings if f.rule_id == "module-unpinned"]
    assert len(warnings) == 1
    assert "vpc" in warnings[0].message


def test_pinned_module_is_silent() -> None:
    summary = TerraformSummary(
        modules=[ModuleRef(name="vpc", source="terraform-aws-modules/vpc/aws", version="5.0.0")],
    )
    report = _run_with_summary(summary)
    assert [f for f in report.warnings if f.rule_id == "module-unpinned"] == []


def test_no_terraform_files_fires_info_on_fully_empty_summary() -> None:
    summary = TerraformSummary()  # all defaults — empty
    report = _run_with_summary(summary)
    infos = report.infos
    assert len(infos) == 1
    assert infos[0].rule_id == "no-terraform-files"


def test_no_terraform_files_does_not_fire_when_anything_is_present() -> None:
    """A repo with just a single resource shouldn't fire the
    no-files info — it has files, just maybe a sparse one."""
    summary = TerraformSummary(
        resources=[ResourceRef(type="random_id", name="r")],
    )
    report = _run_with_summary(summary)
    assert [f for f in report.infos if f.rule_id == "no-terraform-files"] == []


def test_findings_sorted_errors_first_then_warns_then_infos() -> None:
    summary = TerraformSummary(
        providers=[
            ProviderRef(name="aws", source="hashicorp/aws", version=None),  # unpinned (warn)
            ProviderRef(name="cloudflare", source=None, version=None),  # undeclared (error)
        ],
        resources=[ResourceRef(type="cloudflare_zone", name="z")],
    )
    report = _run_with_summary(summary)
    severities = [f.severity for f in report.findings]
    assert severities == [Severity.ERROR, Severity.WARN]


# ── compute_exit_code ────────────────────────────────────────────────


def test_exit_code_zero_when_no_findings() -> None:
    report = LintReport(findings=[], repo_path="/x")
    assert compute_exit_code(report, Severity.ERROR) == 0
    assert compute_exit_code(report, Severity.WARN) == 0
    assert compute_exit_code(report, Severity.INFO) == 0


def test_exit_code_two_on_error_regardless_of_threshold() -> None:
    report = LintReport(
        findings=[LintFinding(rule_id="provider-undeclared", severity=Severity.ERROR, message="m")],
        repo_path="/x",
    )
    assert compute_exit_code(report, Severity.ERROR) == 2
    assert compute_exit_code(report, Severity.WARN) == 2
    assert compute_exit_code(report, Severity.INFO) == 2


def test_exit_code_zero_for_warn_with_fail_on_error() -> None:
    """Default behaviour: warnings don't fail the build."""
    report = LintReport(
        findings=[LintFinding(rule_id="provider-unpinned", severity=Severity.WARN, message="m")],
        repo_path="/x",
    )
    assert compute_exit_code(report, Severity.ERROR) == 0


def test_exit_code_one_for_warn_with_fail_on_warn() -> None:
    report = LintReport(
        findings=[LintFinding(rule_id="provider-unpinned", severity=Severity.WARN, message="m")],
        repo_path="/x",
    )
    assert compute_exit_code(report, Severity.WARN) == 1


def test_exit_code_one_for_info_with_fail_on_info() -> None:
    report = LintReport(
        findings=[LintFinding(rule_id="no-terraform-files", severity=Severity.INFO, message="m")],
        repo_path="/x",
    )
    assert compute_exit_code(report, Severity.INFO) == 1
    # ...but `fail_on=warn` ignores info findings.
    assert compute_exit_code(report, Severity.WARN) == 0


# ── render_text ──────────────────────────────────────────────────────


def test_render_text_empty_report() -> None:
    report = LintReport(findings=[], repo_path="./infra")
    out = render_text(report)
    assert "No findings" in out
    assert "./infra" in out


def test_render_text_includes_counts_header() -> None:
    report = LintReport(
        findings=[
            LintFinding(rule_id="r1", severity=Severity.ERROR, message="e"),
            LintFinding(rule_id="r2", severity=Severity.WARN, message="w1"),
            LintFinding(rule_id="r3", severity=Severity.WARN, message="w2"),
        ],
        repo_path="./infra",
    )
    out = render_text(report)
    assert "1 error(s)" in out
    assert "2 warning(s)" in out
    assert "0 info" in out


def test_render_text_uses_uppercase_severity_prefix() -> None:
    """`grep -E '^ERROR'` should work to filter the output in CI logs."""
    report = LintReport(
        findings=[LintFinding(rule_id="r", severity=Severity.ERROR, message="boom")],
        repo_path="/",
    )
    out = render_text(report)
    assert "ERROR  [r]" in out


# ── render_json ──────────────────────────────────────────────────────


def test_render_json_schema_and_summary() -> None:
    report = LintReport(
        findings=[
            LintFinding(rule_id="r1", severity=Severity.ERROR, message="e", path="p1"),
            LintFinding(rule_id="r2", severity=Severity.WARN, message="w"),
        ],
        repo_path="./infra",
    )
    payload = json_module.loads(render_json(report))
    assert payload["schema"] == "iac-cartographer.lint.v1"
    assert payload["repo_path"] == "./infra"
    assert payload["summary"] == {"error": 1, "warn": 1, "info": 0}
    assert len(payload["findings"]) == 2
    assert payload["findings"][0]["rule_id"] == "r1"
    assert payload["findings"][0]["severity"] == "error"
    assert payload["findings"][0]["path"] == "p1"


def test_render_json_is_machine_parseable_even_when_empty() -> None:
    report = LintReport(findings=[], repo_path="./infra")
    payload = json_module.loads(render_json(report))
    assert payload["summary"] == {"error": 0, "warn": 0, "info": 0}
    assert payload["findings"] == []


# ── render_github ────────────────────────────────────────────────────


def test_render_github_empty_report_emits_nothing() -> None:
    report = LintReport(findings=[], repo_path="./infra")
    assert render_github(report) == ""


def test_render_github_emits_annotation_per_finding() -> None:
    report = LintReport(
        findings=[
            LintFinding(rule_id="provider-undeclared", severity=Severity.ERROR, message="boom"),
            LintFinding(rule_id="provider-unpinned", severity=Severity.WARN, message="loose"),
            LintFinding(rule_id="no-terraform-files", severity=Severity.INFO, message="empty"),
        ],
        repo_path="./infra",
    )
    out = render_github(report).strip().splitlines()
    assert out[0].startswith("::error file=./infra,title=provider-undeclared::")
    assert out[1].startswith("::warning file=./infra,title=provider-unpinned::")
    assert out[2].startswith("::notice file=./infra,title=no-terraform-files::")


def test_render_github_escapes_annotation_special_chars() -> None:
    """Newlines, carriage returns, colons, and percent signs all have
    special meaning in the annotation parser — we URL-encode them."""
    report = LintReport(
        findings=[
            LintFinding(
                rule_id="r",
                severity=Severity.ERROR,
                message="line1\nline2 with: colon and %",
            ),
        ],
        repo_path="./x",
    )
    out = render_github(report)
    assert "%0A" in out  # \n
    assert "%3A" in out  # :
    assert "%25" in out  # %
    # Raw control chars stay out.
    assert "\n" in out  # only the trailing newline
    assert out.count("\n") == 1


# ── render() dispatch ────────────────────────────────────────────────


def test_render_dispatches_text_format() -> None:
    report = LintReport(findings=[], repo_path="./x")
    assert "No findings" in render(report, "text")


def test_render_dispatches_json_format() -> None:
    report = LintReport(findings=[], repo_path="./x")
    payload = json_module.loads(render(report, "json"))
    assert payload["schema"] == "iac-cartographer.lint.v1"


def test_render_dispatches_github_format() -> None:
    report = LintReport(
        findings=[LintFinding(rule_id="r", severity=Severity.ERROR, message="m")],
        repo_path="./x",
    )
    out = render(report, "github")
    assert out.startswith("::error")


def test_render_case_insensitive_format_arg() -> None:
    report = LintReport(findings=[], repo_path="./x")
    assert render(report, "TEXT") == render(report, "text")
    assert render(report, "JSON") == render(report, "json")


def test_render_rejects_unknown_format() -> None:
    report = LintReport(findings=[], repo_path="./x")
    with pytest.raises(ValueError, match="unknown lint format"):
        render(report, "xml")


# ── CLI dispatcher ───────────────────────────────────────────────────


def test_cli_lint_mode_returns_exit_zero_on_clean_repo() -> None:
    from iac_cartographer.cli import main

    clean_summary = TerraformSummary(
        providers=[ProviderRef(name="aws", source="hashicorp/aws", version=">= 5.0")],
        resources=[ResourceRef(type="aws_instance", name="vm")],
    )
    with patch("iac_cartographer.lint.run_terraform_docs", return_value=clean_summary):
        code = main(["--lint", "./fake"])
    assert code == 0


def test_cli_lint_mode_returns_exit_two_on_errors() -> None:
    from iac_cartographer.cli import main

    dirty_summary = TerraformSummary(
        providers=[ProviderRef(name="cloudflare", source=None, version=None)],
        resources=[ResourceRef(type="cloudflare_zone", name="z")],
    )
    with patch("iac_cartographer.lint.run_terraform_docs", return_value=dirty_summary):
        code = main(["--lint", "./fake"])
    assert code == 2


def test_cli_lint_mode_respects_fail_on_warn() -> None:
    from iac_cartographer.cli import main

    warn_summary = TerraformSummary(
        providers=[ProviderRef(name="aws", source="hashicorp/aws", version=None)],
    )
    with patch("iac_cartographer.lint.run_terraform_docs", return_value=warn_summary):
        # Default --fail-on=error → exit 0 (warn doesn't trigger).
        assert main(["--lint", "./fake"]) == 0
        # With --fail-on=warn → exit 1.
        assert main(["--lint", "./fake", "--fail-on=warn"]) == 1


def test_cli_lint_mode_format_json_outputs_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    from iac_cartographer.cli import main

    summary = TerraformSummary(
        providers=[ProviderRef(name="aws", source="hashicorp/aws", version=None)],
    )
    with patch("iac_cartographer.lint.run_terraform_docs", return_value=summary):
        main(["--lint", "./fake", "--format=json"])
    captured = capsys.readouterr()
    parsed = json_module.loads(captured.out)
    assert parsed["schema"] == "iac-cartographer.lint.v1"
    assert parsed["summary"]["warn"] == 1


def test_cli_lint_mode_format_github_emits_annotations(capsys: pytest.CaptureFixture[str]) -> None:
    from iac_cartographer.cli import main

    summary = TerraformSummary(
        providers=[ProviderRef(name="cloudflare", source=None, version=None)],
        resources=[ResourceRef(type="cloudflare_zone", name="z")],
    )
    with patch("iac_cartographer.lint.run_terraform_docs", return_value=summary):
        main(["--lint", "./fake", "--format=github"])
    captured = capsys.readouterr()
    # `Path("./fake")` normalises to `"fake"` — the renderer uses the
    # normalised form so the annotation file= stays operator-visible
    # regardless of how the path was typed on the CLI.
    assert "::error file=fake,title=provider-undeclared::" in captured.out


def test_cli_lint_mode_is_mutually_exclusive_with_once() -> None:
    """argparse should reject `--once --lint X` (the modes are
    declared in a mutually-exclusive group)."""
    from iac_cartographer.cli import main

    with pytest.raises(SystemExit):
        main(["--once", "--lint", "./fake"])


# Keep test_lint.py self-contained — no pytest configuration overrides.
_ = Path
