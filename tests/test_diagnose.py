"""Tests for `iac-cartographer --diagnose`.

The diagnose module runs offline — no live API calls — so the tests
build `AppConfig` objects directly and assert on per-probe verdicts.
The only probe that touches the outside world is `check_terraform_docs`
(shells out to the binary); it's exercised via monkeypatched
`shutil.which` + `subprocess.run`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from iac_cartographer import diagnose
from iac_cartographer.cli import _load_secrets
from iac_cartographer.diagnose import (
    CheckResult,
    DiagnoseReport,
    Status,
    check_discovery,
    check_discovery_live,
    check_llm,
    check_llm_live,
    check_notifications,
    check_optional_deps,
    check_publisher_live,
    check_publisher_target,
    check_secrets_live,
    check_terraform_docs,
    render,
    run_diagnose,
)
from iac_cartographer.models import AppConfig
from iac_cartographer.secrets import EnvSecretsProvider


def _config(**overrides) -> AppConfig:
    """Minimal valid config with a file discovery source + markdown
    publisher (the all-base-install, no-credentials combination).
    Override any top-level section via kwargs."""
    base = {
        "discovery": {"repos_file": "./repos.yaml"},
        "secrets": {"backend": "env"},
        "llm": {"backend": "bedrock"},
        "publisher": {"kind": "markdown"},
        "markdown": {"output_dir": "/tmp"},
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


# ── exit code + report aggregation ───────────────────────────────────


def test_exit_code_is_zero_when_all_ok() -> None:
    report = DiagnoseReport(checks=[CheckResult("a", Status.OK, "fine"), CheckResult("b", Status.OK, "fine")])
    assert report.exit_code == 0


def test_exit_code_is_one_for_warnings() -> None:
    report = DiagnoseReport(checks=[CheckResult("a", Status.OK, "x"), CheckResult("b", Status.WARN, "meh")])
    assert report.exit_code == 1


def test_exit_code_is_two_for_any_failure() -> None:
    report = DiagnoseReport(
        checks=[
            CheckResult("a", Status.OK, "x"),
            CheckResult("b", Status.WARN, "meh"),
            CheckResult("c", Status.FAIL, "boom"),
        ]
    )
    assert report.exit_code == 2


def test_skip_does_not_degrade_exit_code() -> None:
    """A SKIP (unconfigured-but-optional) is not a failure."""
    report = DiagnoseReport(checks=[CheckResult("a", Status.OK, "x"), CheckResult("b", Status.SKIP, "n/a")])
    assert report.exit_code == 0


# ── terraform-docs probe ─────────────────────────────────────────────


def test_terraform_docs_missing_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: None)
    r = check_terraform_docs()
    assert r.status == Status.FAIL
    assert "not found" in r.detail
    assert r.hint and "terraform-docs.io" in r.hint


def test_terraform_docs_pinned_version_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/local/bin/terraform-docs")
    monkeypatch.setattr(
        diagnose.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="terraform-docs version v0.20.0", stderr=""),
    )
    r = check_terraform_docs()
    assert r.status == Status.OK
    assert r.detail == "v0.20.0"


def test_terraform_docs_other_version_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/local/bin/terraform-docs")
    monkeypatch.setattr(
        diagnose.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="terraform-docs version v0.24.0", stderr=""),
    )
    r = check_terraform_docs()
    assert r.status == Status.WARN
    assert "v0.24.0" in r.detail
    assert "v0.20.0" in r.detail


def test_terraform_docs_unparseable_version_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/local/bin/terraform-docs")
    monkeypatch.setattr(
        diagnose.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="garbled output no semver", stderr=""),
    )
    r = check_terraform_docs()
    assert r.status == Status.WARN
    assert "unparseable" in r.detail


def test_terraform_docs_timeout_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/local/bin/terraform-docs")

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="terraform-docs", timeout=10)

    monkeypatch.setattr(diagnose.subprocess, "run", _timeout)
    r = check_terraform_docs()
    assert r.status == Status.FAIL
    assert "timed out" in r.detail


# ── optional deps probe ──────────────────────────────────────────────


def test_optional_deps_base_install_is_ok() -> None:
    """bedrock + markdown + no notifications → nothing optional needed."""
    r = check_optional_deps(_config())
    assert r.status == Status.OK
    assert "base-install" in r.detail


def test_optional_deps_missing_notion_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """publisher.kind=notion needs the [notion] extra; simulate it missing."""
    cfg = _config(publisher={"kind": "notion"}, notion={"parent_page_id": "abcd1234"})
    # Pretend notion_client isn't importable.
    real_find_spec = diagnose.importlib.util.find_spec

    def _fake(name, *a, **k):
        if name == "notion_client":
            return None
        return real_find_spec(name, *a, **k)

    monkeypatch.setattr(diagnose.importlib.util, "find_spec", _fake)
    r = check_optional_deps(cfg)
    assert r.status == Status.FAIL
    assert "notion" in r.detail
    assert r.hint and "iac-cartographer[notion]" in r.hint


def test_optional_deps_present_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the extra IS importable, the probe passes."""
    cfg = _config(publisher={"kind": "notion"}, notion={"parent_page_id": "abcd1234"})
    # find_spec returns a truthy spec for everything.
    monkeypatch.setattr(diagnose.importlib.util, "find_spec", lambda name, *a, **k: object())
    r = check_optional_deps(cfg)
    assert r.status == Status.OK
    assert "notion" in r.detail


# ── discovery probe ──────────────────────────────────────────────────


def test_discovery_no_sources_is_fail() -> None:
    cfg = _config(discovery={})  # all empty
    r = check_discovery(cfg)
    assert r.status == Status.FAIL
    assert "no discovery sources" in r.detail


def test_discovery_gitea_without_base_url_is_fail() -> None:
    cfg = _config(discovery={"gitea_orgs": ["acme"]})  # no gitea_base_url
    r = check_discovery(cfg)
    assert r.status == Status.FAIL
    assert "gitea_base_url" in r.detail


def test_discovery_multiple_sources_listed() -> None:
    cfg = _config(
        discovery={
            "github_orgs": ["acme"],
            "gitlab_group_ids": [15, 16],
        }
    )
    r = check_discovery(cfg)
    assert r.status == Status.OK
    assert "github" in r.detail
    assert "gitlab" in r.detail


# ── llm probe ────────────────────────────────────────────────────────


def test_llm_vertex_without_project_is_fail() -> None:
    cfg = _config(llm={"backend": "vertex"})
    r = check_llm(cfg)
    assert r.status == Status.FAIL
    assert "vertex_project_id" in r.detail


def test_llm_azure_without_endpoint_is_fail() -> None:
    cfg = _config(llm={"backend": "azure_openai", "azure_openai_deployment": "gpt-4o"})
    r = check_llm(cfg)
    assert r.status == Status.FAIL
    assert "azure_openai_endpoint" in r.detail


def test_llm_azure_without_deployment_is_fail() -> None:
    cfg = _config(llm={"backend": "azure_openai", "azure_openai_endpoint": "https://x.openai.azure.com"})
    r = check_llm(cfg)
    assert r.status == Status.FAIL
    assert "azure_openai_deployment" in r.detail


def test_llm_bedrock_default_is_ok() -> None:
    r = check_llm(_config())
    assert r.status == Status.OK
    assert "bedrock" in r.detail


# ── publisher probe ──────────────────────────────────────────────────


def test_publisher_confluence_placeholder_site_is_fail() -> None:
    cfg = _config(publisher={"kind": "confluence"}, confluence={"site": "your-org.atlassian.net"})
    r = check_publisher_target(cfg)
    assert r.status == Status.FAIL
    assert "confluence.site" in r.detail


def test_publisher_confluence_real_site_is_ok() -> None:
    cfg = _config(publisher={"kind": "confluence"}, confluence={"site": "acme.atlassian.net"})
    r = check_publisher_target(cfg)
    assert r.status == Status.OK
    assert "acme.atlassian.net" in r.detail


def test_publisher_notion_unset_parent_is_fail() -> None:
    cfg = _config(publisher={"kind": "notion"}, notion={"parent_page_id": ""})
    r = check_publisher_target(cfg)
    assert r.status == Status.FAIL
    assert "parent_page_id" in r.detail


def test_publisher_github_wiki_unset_is_fail() -> None:
    cfg = _config(publisher={"kind": "github_wiki"}, github_wiki={"owner": "", "repo": ""})
    r = check_publisher_target(cfg)
    assert r.status == Status.FAIL
    assert "owner" in r.detail or "repo" in r.detail


def test_publisher_markdown_writable_dir_is_ok(tmp_path: Path) -> None:
    cfg = _config(publisher={"kind": "markdown"}, markdown={"output_dir": str(tmp_path / "out")})
    r = check_publisher_target(cfg)
    assert r.status == Status.OK


def test_publisher_markdown_unwritable_parent_is_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(publisher={"kind": "markdown"}, markdown={"output_dir": str(tmp_path / "sub" / "out")})
    # Force os.access to report the nearest existing parent as not writable.
    monkeypatch.setattr(diagnose.os, "access", lambda *a, **k: False)
    r = check_publisher_target(cfg)
    assert r.status == Status.FAIL
    assert "not writable" in r.detail


def test_publisher_json_uses_json_output_attr(tmp_path: Path) -> None:
    """The `json` publisher's config block is `json_output` on AppConfig
    (avoids shadowing BaseModel.json()) — probe must read the right attr."""
    cfg = _config(publisher={"kind": "json"}, json={"output_dir": str(tmp_path)})
    r = check_publisher_target(cfg)
    assert r.status == Status.OK
    assert "json" in r.detail


# ── notifications probe ──────────────────────────────────────────────


def test_notifications_empty_is_skip() -> None:
    r = check_notifications(_config())
    assert r.status == Status.SKIP


def test_notifications_configured_is_ok() -> None:
    cfg = _config(notifications=[{"kind": "stdout"}])
    r = check_notifications(cfg)
    assert r.status == Status.OK
    assert "stdout" in r.detail


# ── top-level orchestration ──────────────────────────────────────────


def test_run_diagnose_returns_early_on_bad_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A config that fails to load short-circuits — only the
    terraform-docs + config checks run, nothing downstream."""
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/local/bin/terraform-docs")
    monkeypatch.setattr(
        diagnose.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="v0.20.0", stderr=""),
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text("discovery: {gitea_orgs: [acme]}\nbogus_top_level_key: true\n")
    report = run_diagnose(str(bad))
    names = [c.name for c in report.checks]
    assert "config" in names
    config_check = next(c for c in report.checks if c.name == "config")
    assert config_check.status == Status.FAIL
    # Downstream probes did NOT run.
    assert "discovery" not in names
    assert report.exit_code == 2


def test_run_diagnose_full_green_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/local/bin/terraform-docs")
    monkeypatch.setattr(
        diagnose.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="terraform-docs version v0.20.0", stderr=""),
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "discovery:\n"
        "  github_orgs: [acme]\n"
        "secrets:\n"
        "  backend: env\n"
        "llm:\n"
        "  backend: bedrock\n"
        "publisher:\n"
        "  kind: markdown\n"
        f"markdown:\n  output_dir: {tmp_path}/out\n"
    )
    report = run_diagnose(str(cfg))
    assert report.exit_code == 0
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["terraform-docs"] == Status.OK
    assert statuses["config"] == Status.OK
    assert statuses["discovery"] == Status.OK
    assert statuses["publisher"] == Status.OK


def test_probe_exception_becomes_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run` wraps any probe exception into a FAIL result rather than
    propagating — one broken probe shouldn't abort the whole report."""

    def _boom() -> CheckResult:
        raise RuntimeError("kaboom")

    r = diagnose._run("explode", _boom)
    assert r.status == Status.FAIL
    assert "kaboom" in r.detail


# ── render ───────────────────────────────────────────────────────────


def test_render_includes_glyphs_and_summary() -> None:
    report = DiagnoseReport(
        checks=[
            CheckResult("terraform-docs", Status.OK, "v0.20.0"),
            CheckResult("publisher", Status.FAIL, "broken", hint="fix it"),
        ]
    )
    out = render(report)
    assert "✓" in out
    assert "✗" in out
    assert "→ fix it" in out  # hint shown for non-OK
    assert "1 ok, 1 fail" in out
    assert "At least one check failed" in out


def test_render_hint_suppressed_for_ok_checks() -> None:
    report = DiagnoseReport(checks=[CheckResult("x", Status.OK, "fine", hint="should-not-show")])
    out = render(report)
    assert "should-not-show" not in out


def test_render_warn_only_footer() -> None:
    report = DiagnoseReport(checks=[CheckResult("x", Status.OK, "ok"), CheckResult("y", Status.WARN, "meh")])
    out = render(report)
    assert "Warnings only" in out
    assert "1 ok, 1 warn" in out


def test_render_all_ok_footer_and_skip_count() -> None:
    report = DiagnoseReport(checks=[CheckResult("x", Status.OK, "ok"), CheckResult("y", Status.SKIP, "n/a")])
    out = render(report)
    assert "All checks passed" in out
    assert "1 skip" in out


def test_optional_deps_multiple_missing_groups_in_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """When two distinct extras groups are missing, the hint lists both
    in a single `pip install iac-cartographer[a,b]` invocation."""
    cfg = _config(
        llm={"backend": "openai"},
        publisher={"kind": "notion"},
        notion={"parent_page_id": "abcd1234"},
    )

    def _all_missing(name, *a, **k):
        return None

    monkeypatch.setattr(diagnose.importlib.util, "find_spec", _all_missing)
    r = check_optional_deps(cfg)
    assert r.status == Status.FAIL
    # Both groups present in the combined install hint.
    assert r.hint and "notion" in r.hint and "openai" in r.hint


# ── live probes ──────────────────────────────────────────────────────
#
# Everything below runs OFFLINE: env-backed secrets via monkeypatched env
# vars, httpx clients mocked with respx, subprocess/git monkeypatched. No
# real network ever fires.


def _set_core_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the env vars the `env` secrets backend reads for the four
    always-required credentials (confluence / gitlab / github / slack)."""
    monkeypatch.setenv(
        "IAC_CARTOGRAPHER_SECRET_CONFLUENCE",
        json.dumps({"email": "bot@x.test", "api_token": "ATATT"}),
    )
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_GITLAB", json.dumps({"token": "glpat-AAAA"}))
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_GITHUB", json.dumps({"token": "ghp_AAAA"}))
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_SLACK", json.dumps({"bot_token": "xoxb-AAAA"}))


# ── secrets-live ─────────────────────────────────────────────────────


def test_secrets_live_resolves_required_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_core_secret_env(monkeypatch)
    cfg = _config(secrets={"backend": "env"}, discovery={"github_orgs": ["acme"]})
    result, secrets = check_secrets_live(cfg)
    assert result.status == Status.OK
    assert "resolved via env" in result.detail
    assert secrets is not None
    assert secrets.github.token == "ghp_AAAA"


def test_secrets_live_missing_secret_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only set three of the four required secrets — slack is missing.
    monkeypatch.setenv(
        "IAC_CARTOGRAPHER_SECRET_CONFLUENCE",
        json.dumps({"email": "bot@x.test", "api_token": "ATATT"}),
    )
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_GITLAB", json.dumps({"token": "glpat"}))
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_GITHUB", json.dumps({"token": "ghp"}))
    monkeypatch.delenv("IAC_CARTOGRAPHER_SECRET_SLACK", raising=False)
    cfg = _config(secrets={"backend": "env"}, discovery={"github_orgs": ["acme"]})
    result, secrets = check_secrets_live(cfg)
    assert result.status == Status.FAIL
    assert secrets is None
    assert result.hint and "env" in result.hint


def test_secrets_live_bad_provider_config_is_fail() -> None:
    # vault backend with no vault_addr → build_provider raises ConfigError.
    cfg = _config(secrets={"backend": "vault"}, discovery={"github_orgs": ["acme"]})
    result, secrets = check_secrets_live(cfg)
    assert result.status == Status.FAIL
    assert secrets is None
    assert "vault" in result.detail


# Shared helper that builds a real LoadedSecrets for the downstream probes.
def _build_secrets(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    _set_core_secret_env(monkeypatch)
    return _load_secrets(EnvSecretsProvider(), "bedrock")


# ── discovery-live ───────────────────────────────────────────────────


@respx.mock
def test_discovery_live_github_auth_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _build_secrets(monkeypatch)
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(200, json={"login": "bot"}))
    cfg = _config(discovery={"github_orgs": ["acme"]})
    r = check_discovery_live(cfg, secrets)
    assert r.status == Status.OK
    assert "github" in r.detail


@respx.mock
def test_discovery_live_bad_token_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _build_secrets(monkeypatch)
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    cfg = _config(discovery={"github_orgs": ["acme"]})
    r = check_discovery_live(cfg, secrets)
    assert r.status == Status.FAIL
    assert "github auth failed" in r.detail


def test_discovery_live_file_only_is_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _build_secrets(monkeypatch)
    cfg = _config(discovery={"repos_file": "./repos.yaml"})
    r = check_discovery_live(cfg, secrets)
    assert r.status == Status.SKIP
    assert "file source only" in r.detail


# ── llm-live ─────────────────────────────────────────────────────────


@respx.mock
def test_llm_live_ollama_tags_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _build_secrets(monkeypatch)
    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "llama3"}, {"name": "qwen"}]})
    )
    cfg = _config(llm={"backend": "ollama"})
    r = check_llm_live(cfg, secrets)
    assert r.status == Status.OK
    assert "2 model(s)" in r.detail


@respx.mock
def test_llm_live_ollama_unreachable_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _build_secrets(monkeypatch)
    respx.get("http://localhost:11434/api/tags").mock(return_value=httpx.Response(503))
    cfg = _config(llm={"backend": "ollama"})
    r = check_llm_live(cfg, secrets)
    assert r.status == Status.FAIL
    assert "503" in r.detail


def test_llm_live_bedrock_is_cost_safe_build_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Bedrock probe constructs the client but NEVER runs a completion."""
    secrets = _build_secrets(monkeypatch)
    cfg = _config(llm={"backend": "bedrock"})
    r = check_llm_live(cfg, secrets)
    assert r.status == Status.OK
    assert "cost-safe" in r.detail
    assert "no completion" in r.detail


# ── publisher-live ───────────────────────────────────────────────────


def test_publisher_live_markdown_is_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _build_secrets(monkeypatch)
    cfg = _config(publisher={"kind": "markdown"}, markdown={"output_dir": "/tmp"})
    r = check_publisher_live(cfg, secrets)
    assert r.status == Status.SKIP
    assert "no remote target" in r.detail


@respx.mock
def test_publisher_live_confluence_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _build_secrets(monkeypatch)
    respx.get("https://acme.atlassian.net/wiki/api/v2/pages/12345").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "12345",
                "title": "IaC Inventory",
                "version": {"number": 3},
                "body": {"atlas_doc_format": {"value": "{}"}},
            },
        )
    )
    cfg = _config(
        publisher={"kind": "confluence"},
        confluence={"site": "acme.atlassian.net", "parent_page_id": "12345"},
    )
    r = check_publisher_live(cfg, secrets)
    assert r.status == Status.OK
    assert "IaC Inventory" in r.detail


@respx.mock
def test_publisher_live_notion_bad_token_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_core_secret_env(monkeypatch)
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_NOTION", json.dumps({"integration_token": "secret_X"}))
    secrets = _load_secrets(EnvSecretsProvider(), "bedrock", need_notion=True)
    respx.get("https://api.notion.com/v1/pages/abcd1234").mock(return_value=httpx.Response(401))
    cfg = _config(publisher={"kind": "notion"}, notion={"parent_page_id": "abcd1234"})
    r = check_publisher_live(cfg, secrets)
    assert r.status == Status.FAIL
    assert "401" in r.detail


def test_publisher_live_github_wiki_ls_remote_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _build_secrets(monkeypatch)
    monkeypatch.setattr(
        diagnose.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="abc123\tHEAD", stderr=""),
    )
    cfg = _config(publisher={"kind": "github_wiki"}, github_wiki={"owner": "acme", "repo": "infra"})
    r = check_publisher_live(cfg, secrets)
    assert r.status == Status.OK
    assert "acme/infra.wiki" in r.detail


def test_publisher_live_github_wiki_ls_remote_fail_redacts_token(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = _build_secrets(monkeypatch)

    def _fail(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return subprocess.CompletedProcess(a, 128, stdout="", stderr="fatal: ghp_AAAA auth failed for repo")

    monkeypatch.setattr(diagnose.subprocess, "run", _fail)
    cfg = _config(publisher={"kind": "github_wiki"}, github_wiki={"owner": "acme", "repo": "infra"})
    r = check_publisher_live(cfg, secrets)
    assert r.status == Status.FAIL
    assert "ghp_AAAA" not in r.detail  # token redacted
    assert "<TOKEN>" in r.detail


# ── run_diagnose orchestration with --live ───────────────────────────


def _green_terraform_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/local/bin/terraform-docs")
    monkeypatch.setattr(
        diagnose.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="terraform-docs version v0.20.0", stderr=""),
    )


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "discovery:\n"
        "  github_orgs: [acme]\n"
        "secrets:\n"
        "  backend: env\n"
        "llm:\n"
        "  backend: bedrock\n"
        "publisher:\n"
        "  kind: markdown\n"
        f"markdown:\n  output_dir: {tmp_path}/out\n"
    )
    return cfg


def test_run_diagnose_without_live_runs_no_live_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--live` off → identical probe set to today (no *-live checks)."""
    _green_terraform_docs(monkeypatch)
    report = run_diagnose(str(_write_config(tmp_path)))
    names = [c.name for c in report.checks]
    assert not any(n.endswith("-live") for n in names)


@respx.mock
def test_run_diagnose_live_full_green(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _green_terraform_docs(monkeypatch)
    _set_core_secret_env(monkeypatch)
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(200, json={"login": "bot"}))
    report = run_diagnose(str(_write_config(tmp_path)), live=True)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["secrets-live"] == Status.OK
    assert statuses["discovery-live"] == Status.OK
    assert statuses["llm-live"] == Status.OK  # bedrock build-only
    assert statuses["publisher-live"] == Status.SKIP  # markdown has no remote
    assert report.exit_code in (0, 1)


def test_run_diagnose_live_skips_downstream_when_secrets_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If secrets-live can't resolve, discovery/llm/publisher live probes
    are explicitly skipped (never attempted with no credentials)."""
    _green_terraform_docs(monkeypatch)
    # No secret env vars set → secrets-live fails.
    for var in (
        "IAC_CARTOGRAPHER_SECRET_CONFLUENCE",
        "IAC_CARTOGRAPHER_SECRET_GITLAB",
        "IAC_CARTOGRAPHER_SECRET_GITHUB",
        "IAC_CARTOGRAPHER_SECRET_SLACK",
    ):
        monkeypatch.delenv(var, raising=False)
    report = run_diagnose(str(_write_config(tmp_path)), live=True)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["secrets-live"] == Status.FAIL
    assert statuses["discovery-live"] == Status.SKIP
    assert statuses["llm-live"] == Status.SKIP
    assert statuses["publisher-live"] == Status.SKIP


def test_run_diagnose_live_skips_live_when_offline_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A component whose OFFLINE probe failed doesn't get a live probe."""
    _green_terraform_docs(monkeypatch)
    _set_core_secret_env(monkeypatch)
    # vertex backend with no project_id → offline llm probe FAILS, so the
    # llm-live probe must be skipped (not attempted).
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "discovery:\n"
        "  github_orgs: [acme]\n"
        "secrets:\n"
        "  backend: env\n"
        "llm:\n"
        "  backend: vertex\n"  # no vertex_project_id → offline llm fail
        "publisher:\n"
        "  kind: markdown\n"
        f"markdown:\n  output_dir: {tmp_path}/out\n"
    )
    with respx.mock:
        respx.get("https://api.github.com/user").mock(return_value=httpx.Response(200, json={"login": "bot"}))
        report = run_diagnose(str(cfg), live=True)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["llm"] == Status.FAIL
    assert statuses["llm-live"] == Status.SKIP
    assert "offline llm probe failed" in next(c.detail for c in report.checks if c.name == "llm-live")
