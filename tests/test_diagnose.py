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
    # Config uses github discovery → the github secret is REQUIRED. Delete
    # it (but leave the others) to force a hard failure. (Note: slack on the
    # empty-notifications path is now optional, so a missing slack secret
    # is NOT a failure — we must drop a genuinely-required one.)
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_GITLAB", json.dumps({"token": "glpat"}))
    monkeypatch.delenv("IAC_CARTOGRAPHER_SECRET_GITHUB", raising=False)
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


# ── secrets-required (offline per-secret status — #100) ───────────────


def _required_by(rows: list[CheckResult]) -> dict[str, CheckResult]:
    """Re-key the list of `secrets.<name>` rows by short name for assertions."""
    return {r.name.removeprefix("secrets."): r for r in rows}


def test_check_secrets_required_markdown_only_github_only() -> None:
    """A Markdown publisher + a single GitHub discovery source ⇒ exactly
    one credential required (`github`). Every other secret is `not active`."""
    from iac_cartographer.diagnose import check_secrets_required

    cfg = _config(discovery={"github_orgs": ["acme"]}, publisher={"kind": "markdown"})
    rows = _required_by(check_secrets_required(cfg))

    assert rows["github"].status == Status.OK
    assert "discovery.github_orgs" in rows["github"].detail

    for name in (
        "confluence",
        "notion",
        "gitlab",
        "bitbucket",
        "gitea",
        "anthropic",
        "openai",
        "azure_openai",
        "webhook",
        "teams",
        "email",
        "pagerduty",
        "opsgenie",
        "discord",
    ):
        assert rows[name].status == Status.SKIP, f"{name} should be inactive but isn't"
        assert rows[name].detail == "not active"


def test_check_secrets_required_json_only_with_curated_repos_file() -> None:
    """A JSON publisher + `repos_file` (no other discovery sources) forces
    BOTH gitlab + github credentials — the file can list repos on either
    host and the clone path splices a per-host token. Everything else is
    inactive."""
    from iac_cartographer.diagnose import check_secrets_required

    cfg = _config(
        discovery={"repos_file": "./repos.yaml"},
        publisher={"kind": "json"},
        json_output={"output_dir": "/tmp/json"},
    )
    rows = _required_by(check_secrets_required(cfg))

    assert rows["gitlab"].status == Status.OK
    assert "discovery.repos_file" in rows["gitlab"].detail
    assert rows["github"].status == Status.OK
    assert "discovery.repos_file" in rows["github"].detail
    assert rows["confluence"].status == Status.SKIP


def test_check_secrets_required_full_confluence_deployment() -> None:
    """The classic Confluence + GitLab + GitHub + Slack deployment marks
    each subsystem's credential as `required`."""
    from iac_cartographer.diagnose import check_secrets_required

    cfg = _config(
        discovery={"gitlab_group_ids": [15], "github_orgs": ["acme"]},
        publisher={"kind": "confluence"},
        notifications=[{"kind": "slack"}],
    )
    rows = _required_by(check_secrets_required(cfg))

    assert rows["confluence"].status == Status.OK
    assert rows["gitlab"].status == Status.OK
    assert rows["github"].status == Status.OK
    assert rows["slack"].status == Status.OK
    assert "notifications[].kind=slack" in rows["slack"].detail


def test_check_secrets_required_slack_optional_on_legacy_path() -> None:
    """Empty `notifications: []` is the legacy single-Slack path: the
    Slack secret is OPTIONAL — loaded if present, silent if absent."""
    from iac_cartographer.diagnose import check_secrets_required

    cfg = _config(discovery={"github_orgs": ["acme"]}, publisher={"kind": "markdown"})
    rows = _required_by(check_secrets_required(cfg))
    assert rows["slack"].status == Status.OK
    assert "optional" in rows["slack"].detail.lower()


def test_check_secrets_required_hybrid_publisher_and_channels() -> None:
    """Hybrid deployment: github_wiki publisher (which reuses the github
    credential) + multiple notification channels — exactly the credentials
    those subsystems trigger are required, nothing more."""
    from iac_cartographer.diagnose import check_secrets_required

    cfg = _config(
        discovery={"github_orgs": ["acme"]},
        publisher={"kind": "github_wiki"},
        github_wiki={"owner": "acme", "repo": "infra-docs.wiki"},
        notifications=[{"kind": "teams"}, {"kind": "pagerduty"}],
    )
    rows = _required_by(check_secrets_required(cfg))

    assert rows["github"].status == Status.OK
    # github_wiki should appear in the trigger string alongside the org list.
    assert "publisher.kind=github_wiki" in rows["github"].detail
    assert rows["teams"].status == Status.OK
    assert rows["pagerduty"].status == Status.OK
    # And channels NOT configured are still inactive.
    assert rows["slack"].status == Status.SKIP
    assert rows["discord"].status == Status.SKIP


# ── check_live_state (#98) ────────────────────────────────────────────


def test_check_live_state_none_backend_skips() -> None:
    """Default `backend: "none"` is the no-op path — SKIP, not FAIL."""
    from iac_cartographer.diagnose import check_live_state

    r = check_live_state(_config())
    assert r.status == Status.SKIP
    assert "backend=none" in r.detail


def test_check_live_state_tfc_without_organization_fails() -> None:
    from iac_cartographer.diagnose import check_live_state

    r = check_live_state(_config(live_state={"backend": "tfc"}))
    assert r.status == Status.FAIL
    assert "organization is unset" in r.detail


def test_check_live_state_tfc_ok_reports_staleness_threshold() -> None:
    from iac_cartographer.diagnose import check_live_state

    r = check_live_state(_config(live_state={"backend": "tfc", "organization": "acme"}))
    assert r.status == Status.OK
    assert "tfc" in r.detail and "acme" in r.detail
    # Default staleness threshold is surfaced.
    assert "threshold=2" in r.detail


def test_check_live_state_tfc_ok_reports_staleness_disabled() -> None:
    from iac_cartographer.diagnose import check_live_state

    r = check_live_state(
        _config(
            live_state={
                "backend": "tfc",
                "organization": "acme",
                "staleness": {"enabled": False},
            }
        )
    )
    assert r.status == Status.OK
    assert "staleness disabled" in r.detail


def test_check_secrets_required_tfc_active_when_live_state_backend_is_tfc() -> None:
    """Flipping `live_state.backend=tfc` moves `secrets.tfc` from
    skip to required."""
    from iac_cartographer.diagnose import check_secrets_required

    default = _required_by(check_secrets_required(_config()))
    assert default["tfc"].status == Status.SKIP

    with_tfc = _required_by(check_secrets_required(_config(live_state={"backend": "tfc", "organization": "acme"})))
    assert with_tfc["tfc"].status == Status.OK
    assert "live_state.backend=tfc" in with_tfc["tfc"].detail


# ── check_live_state (terrakube, #99) ─────────────────────────────────


def test_check_live_state_terrakube_without_organization_fails() -> None:
    from iac_cartographer.diagnose import check_live_state

    r = check_live_state(_config(live_state={"backend": "terrakube", "hostname": "terrakube.example.com"}))
    assert r.status == Status.FAIL
    assert "organization is unset" in r.detail


def test_check_live_state_terrakube_default_hostname_fails() -> None:
    """`hostname` must be customised for Terrakube — the TFC default
    points at app.terraform.io which is the wrong service entirely."""
    from iac_cartographer.diagnose import check_live_state

    r = check_live_state(_config(live_state={"backend": "terrakube", "organization": "acme"}))
    assert r.status == Status.FAIL
    assert "hostname" in r.detail


def test_check_live_state_terrakube_ok_reports_hostname() -> None:
    from iac_cartographer.diagnose import check_live_state

    r = check_live_state(
        _config(
            live_state={
                "backend": "terrakube",
                "organization": "acme",
                "hostname": "terrakube.example.com",
            }
        )
    )
    assert r.status == Status.OK
    assert "terrakube" in r.detail and "acme" in r.detail
    assert "hostname=terrakube.example.com" in r.detail


def test_check_secrets_required_terrakube_active_when_live_state_backend_is_terrakube() -> None:
    from iac_cartographer.diagnose import check_secrets_required

    default = _required_by(check_secrets_required(_config()))
    assert default["terrakube"].status == Status.SKIP

    with_tk = _required_by(
        check_secrets_required(
            _config(
                live_state={
                    "backend": "terrakube",
                    "organization": "acme",
                    "hostname": "terrakube.example.com",
                }
            )
        )
    )
    assert with_tk["terrakube"].status == Status.OK
    assert "live_state.backend=terrakube" in with_tk["terrakube"].detail
    # And the other live-state credential stays skipped.
    assert with_tk["tfc"].status == Status.SKIP


def test_check_secrets_required_llm_backend_swap_changes_credentials() -> None:
    """Flipping `llm.backend` shifts which LLM credential is required —
    bedrock → anthropic should swap `anthropic` from skip to ok and leave
    `openai` / `azure_openai` inactive."""
    from iac_cartographer.diagnose import check_secrets_required

    bedrock = _required_by(check_secrets_required(_config(llm={"backend": "bedrock"})))
    assert bedrock["anthropic"].status == Status.SKIP
    assert bedrock["openai"].status == Status.SKIP
    assert bedrock["azure_openai"].status == Status.SKIP

    anthropic = _required_by(
        check_secrets_required(_config(llm={"backend": "anthropic", "model_id": "claude-sonnet-4-6"}))
    )
    assert anthropic["anthropic"].status == Status.OK
    assert "llm.backend=anthropic" in anthropic["anthropic"].detail
    assert anthropic["openai"].status == Status.SKIP


# Shared helper that builds a real LoadedSecrets for the downstream probes.
# Requests all four core credentials so the discovery / publisher live
# probes have the tokens they exercise (lazy loading skips them otherwise).
def _build_secrets(monkeypatch: pytest.MonkeyPatch):
    _set_core_secret_env(monkeypatch)
    return _load_secrets(
        EnvSecretsProvider(),
        "bedrock",
        need_confluence=True,
        need_gitlab=True,
        need_github=True,
        need_slack=True,
    )


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
    """The Bedrock probe constructs the client but NEVER runs a completion
    in the default (no --probe-llm) mode."""
    secrets = _build_secrets(monkeypatch)
    cfg = _config(llm={"backend": "bedrock"})
    r = check_llm_live(cfg, secrets)
    assert r.status == Status.OK
    assert "cost-safe" in r.detail
    assert "no completion" in r.detail


class _FakeBackend:
    """Records the invoke kwargs so tests can assert max_tokens=1 etc."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke(self, **kwargs: object):
        from iac_cartographer.llm import LLMResponse

        self.calls.append(kwargs)
        return LLMResponse(text="ok", input_tokens=7, output_tokens=1)


def test_llm_live_probe_llm_runs_bounded_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """With probe_llm=True, the probe runs ONE real completion, caps it at
    max_tokens=1, and reports the token counts."""
    secrets = _build_secrets(monkeypatch)
    fake = _FakeBackend()
    # check_llm_live imports _build_llm_backend from cli at call time, so
    # patch it at the source module.
    monkeypatch.setattr("iac_cartographer.cli._build_llm_backend", lambda *a, **k: fake)

    cfg = _config(llm={"backend": "bedrock"})
    r = check_llm_live(cfg, secrets, probe_llm=True)
    assert r.status == Status.OK
    assert "real 1-token probe" in r.detail
    assert "in=7" in r.detail and "out=1" in r.detail
    # Exactly one bounded invocation.
    assert len(fake.calls) == 1
    assert fake.calls[0]["max_tokens"] == 1


def test_llm_live_probe_llm_failure_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising invoke → FAIL with an actionable hint, not a crash."""
    secrets = _build_secrets(monkeypatch)

    class _Boom:
        def invoke(self, **_kwargs: object):
            raise RuntimeError("AccessDeniedException: not authorized to invoke")

    monkeypatch.setattr("iac_cartographer.cli._build_llm_backend", lambda *a, **k: _Boom())
    cfg = _config(llm={"backend": "bedrock"})
    r = check_llm_live(cfg, secrets, probe_llm=True)
    assert r.status == Status.FAIL
    assert "1-token probe failed" in r.detail
    assert r.hint and "invoke permission" in r.hint


def test_llm_live_probe_llm_reports_cost_for_known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known model_id (matches the price-table prefix) → cost line with
    an actual `$0.xxxxxx` figure, computed from the reported token counts."""
    secrets = _build_secrets(monkeypatch)
    fake = _FakeBackend()
    monkeypatch.setattr("iac_cartographer.cli._build_llm_backend", lambda *a, **k: fake)
    cfg = _config(llm={"backend": "bedrock", "model_id": "eu.anthropic.claude-sonnet-4-6"})
    r = check_llm_live(cfg, secrets, probe_llm=True)
    assert r.status == Status.OK
    # Should contain a `$` cost figure, not the fallback wording.
    assert "≈ $0." in r.detail, f"expected a $ cost figure in detail; got: {r.detail!r}"
    assert "negligible (model not in price table)" not in r.detail


def test_llm_live_probe_llm_reports_negligible_for_unknown_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown model_id → no estimate, but the operator still sees a
    cost-band marker so they're not left guessing."""
    secrets = _build_secrets(monkeypatch)
    fake = _FakeBackend()
    monkeypatch.setattr("iac_cartographer.cli._build_llm_backend", lambda *a, **k: fake)
    cfg = _config(llm={"backend": "bedrock", "model_id": "completely-unknown-model-id"})
    r = check_llm_live(cfg, secrets, probe_llm=True)
    assert r.status == Status.OK
    assert "≈ negligible (model not in price table)" in r.detail


def test_llm_live_probe_llm_ollama_still_uses_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama is exempt from the spend probe — even with probe_llm=True it
    keeps the free /api/tags check (no completion)."""
    secrets = _build_secrets(monkeypatch)
    import respx as _respx

    cfg = _config(llm={"backend": "ollama"})
    with _respx.mock:
        _respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3"}]})
        )
        r = check_llm_live(cfg, secrets, probe_llm=True)
    assert r.status == Status.OK
    assert "model(s) listed via /api/tags" in r.detail


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

    def _fail(*a, **k):
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


def test_run_diagnose_live_skips_downstream_when_secrets_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


# ── CLI --probe-llm gating ───────────────────────────────────────────


def test_cli_probe_llm_without_live_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--diagnose --probe-llm` (no --live) warns and drops probe_llm —
    it only extends the live LLM probe, which isn't running."""
    from iac_cartographer import cli

    captured: dict[str, object] = {}

    def _fake_run_diagnose(config_path: str, *, live: bool = False, probe_llm: bool = False):
        captured["live"] = live
        captured["probe_llm"] = probe_llm
        return DiagnoseReport(checks=[CheckResult("x", Status.OK, "ok")])

    monkeypatch.setattr("iac_cartographer.diagnose.run_diagnose", _fake_run_diagnose)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("discovery:\n  github_orgs: [acme]\n", encoding="utf-8")

    rc = cli.main(["--diagnose", "--probe-llm", "--config", str(cfg)])
    assert rc == 0
    assert captured["live"] is False
    assert captured["probe_llm"] is False  # dropped — no --live


def test_cli_probe_llm_with_live_threads_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from iac_cartographer import cli

    captured: dict[str, object] = {}

    def _fake_run_diagnose(config_path: str, *, live: bool = False, probe_llm: bool = False):
        captured["live"] = live
        captured["probe_llm"] = probe_llm
        return DiagnoseReport(checks=[CheckResult("x", Status.OK, "ok")])

    monkeypatch.setattr("iac_cartographer.diagnose.run_diagnose", _fake_run_diagnose)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("discovery:\n  github_orgs: [acme]\n", encoding="utf-8")

    rc = cli.main(["--diagnose", "--live", "--probe-llm", "--config", str(cfg)])
    assert rc == 0
    assert captured["live"] is True
    assert captured["probe_llm"] is True
