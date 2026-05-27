"""Phase 2 tests for iac_cartographer.models — schema strictness + defaults."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from iac_cartographer.models import (
    AppConfig,
    BedrockConfig,
    BedrockNarrative,
    ConfluenceConfig,
    ConfluenceCredentials,
    GithubCredentials,
    GitlabCredentials,
    LLMConfig,
    RepoInventory,
    RepoMetadata,
    ResourceExplanation,
    ResourceRef,
    RunOutcome,
    SlackConfig,
    SlackCredentials,
    TerraformSummary,
    VariableRef,
)


def _make_meta(**overrides: object) -> RepoMetadata:
    defaults: dict[str, object] = {
        "host": "gitlab",
        "full_name": "acme/iac/main-cluster",
        "clone_url": "https://x:y@gitlab.example.com/acme/iac/main-cluster.git",
        "web_url": "https://gitlab.example.com/acme/iac/main-cluster",
        "default_branch": "main",
        "last_commit_sha": "a" * 40,
        "last_commit_at": datetime(2026, 5, 22, tzinfo=UTC),
    }
    defaults.update(overrides)
    return RepoMetadata(**defaults)  # type: ignore[arg-type]


def test_repo_metadata_minimum() -> None:
    meta = _make_meta()
    assert meta.host == "gitlab"
    assert meta.full_name == "acme/iac/main-cluster"


def test_repo_metadata_host_must_be_known() -> None:
    # Sanity check that an outright invalid host literal is rejected;
    # `bitbucket` is now a supported host so use a clearly-unknown value.
    with pytest.raises(ValidationError):
        _make_meta(host="codeberg")  # type: ignore[arg-type]


def test_strict_models_reject_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        RepoMetadata(
            host="gitlab",
            full_name="a/b",
            clone_url="https://x.test/a/b.git",
            web_url="https://x.test/a/b",
            default_branch="main",
            last_commit_sha="a" * 40,
            last_commit_at=datetime(2026, 5, 22, tzinfo=UTC),
            extra_field="not allowed",  # type: ignore[call-arg]
        )


def test_resource_ref_defaults_to_managed_mode() -> None:
    r = ResourceRef(type="aws_iam_role", name="task")
    assert r.mode == "managed"


def test_resource_ref_accepts_data_mode() -> None:
    r = ResourceRef(type="aws_caller_identity", name="current", mode="data")
    assert r.mode == "data"


def test_terraform_summary_empty_defaults() -> None:
    s = TerraformSummary()
    assert s.providers == []
    assert s.resources == []
    assert s.resource_counts_by_type == {}


def test_variable_ref_required_defaults_false() -> None:
    v = VariableRef(name="region")
    assert v.required is False


def test_bedrock_narrative_purpose_min_length() -> None:
    with pytest.raises(ValidationError):
        BedrockNarrative(purpose="too short")


def test_bedrock_narrative_purpose_max_length() -> None:
    long = "x" * 601
    with pytest.raises(ValidationError):
        BedrockNarrative(purpose=long)


def test_bedrock_narrative_minimum() -> None:
    n = BedrockNarrative(
        purpose="A repository that provisions Grafana dashboards, datasources, and alerting rules.",
    )
    assert n.environments == []
    assert n.notable_patterns == []
    assert n.owning_team_guess is None


def test_resource_explanation_max_length() -> None:
    with pytest.raises(ValidationError):
        ResourceExplanation(resource_type="aws_iam_role", why_it_exists="z" * 401)


def test_bedrock_narrative_caps_explanations_at_12() -> None:
    items = [ResourceExplanation(resource_type=f"aws_x_{i}", why_it_exists="ok") for i in range(13)]
    with pytest.raises(ValidationError):
        BedrockNarrative(
            purpose="A repo that does enough things to require many explanations.",
            key_resources_explained=items,
        )


def test_bedrock_narrative_caps_patterns_at_8() -> None:
    items = [f"pattern-{i}" for i in range(9)]
    with pytest.raises(ValidationError):
        BedrockNarrative(
            purpose="A repo that has many notable patterns documented.",
            notable_patterns=items,
        )


def test_repo_inventory_allows_null_narrative() -> None:
    inv = RepoInventory(meta=_make_meta(), summary=TerraformSummary(), narrative=None)
    assert inv.narrative is None


def test_app_config_defaults_load_with_placeholders() -> None:
    """Every sub-config has placeholder defaults so the model can validate
    in tests / dry-runs. Production use MUST override `confluence.site` and
    `confluence.space_key` (the placeholder host won't resolve)."""
    c = AppConfig()
    assert c.llm.model_id == "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert c.llm.max_tokens == 4096
    assert c.confluence.site == "your-org.atlassian.net"
    assert c.confluence.space_key == "DOCS"
    assert c.slack.channel == "#alerts"
    assert c.discovery.github_orgs == []
    assert c.discovery.gitlab_base_url == "https://gitlab.com"


def test_app_config_accepts_partial_yaml() -> None:
    """Real-world YAML overrides only some fields; the rest fall back to
    defaults. Strict mode still rejects unknown keys."""
    raw = {
        "discovery": {"gitlab_group_ids": [1, 2], "deny_repos": ["acme/*-archived"]},
        "confluence": {"site": "acme.atlassian.net", "space_key": "ENG"},
        "slack": {"channel": "#alerts"},
    }
    c = AppConfig.model_validate(raw)
    assert c.discovery.gitlab_group_ids == [1, 2]
    assert c.discovery.deny_repos == ["acme/*-archived"]
    assert c.llm.model_id == "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_app_config_rejects_unknown_top_level_section() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        AppConfig.model_validate({"unknown_section": {}})


def test_sub_config_defaults() -> None:
    assert BedrockConfig().system_prompt_version == "v1"
    assert ConfluenceConfig().parent_page_id_ref == "/iac-cartographer/confluence-parent-id"
    assert SlackConfig().channel == "#alerts"


def test_credentials_parse_dict() -> None:
    c = ConfluenceCredentials.model_validate({"email": "bot@acme.example.com", "api_token": "ATATT3xFfGF0..."})
    assert c.email == "bot@acme.example.com"
    g = GitlabCredentials.model_validate({"token": "glpat-..."})
    assert g.token == "glpat-..."
    h = GithubCredentials.model_validate({"token": "ghp_..."})
    assert h.token == "ghp_..."
    s = SlackCredentials.model_validate({"bot_token": "xoxb-...", "channel_id": "C0X"})
    assert s.bot_token == "xoxb-..."
    assert s.channel_id == "C0X"


def test_credentials_reject_extra_keys() -> None:
    with pytest.raises(ValidationError):
        GitlabCredentials.model_validate({"token": "x", "extra": "y"})


def test_slack_credentials_channel_id_is_optional() -> None:
    """The Slack secret payload only needs `bot_token` — channel routing is
    handled by `SlackConfig.channel` from SSM, not the secret. This lets
    operators seed Slack via just `$DEVOPS_SA_SLACK_TOKEN` without also
    needing a separate channel-ID variable."""
    s = SlackCredentials.model_validate({"bot_token": "xoxb-..."})
    assert s.bot_token == "xoxb-..."
    assert s.channel_id is None


def test_run_outcome_zero_defaults() -> None:
    o = RunOutcome()
    assert o.discovered == 0
    assert o.failed == {}
    assert o.bedrock_tokens_in == 0
    assert o.duration_seconds == 0.0


# ─── AI-H2 hardening: URL rejection in narrative ───────────────────────────


def test_bedrock_narrative_rejects_https_url_in_purpose() -> None:
    with pytest.raises(ValidationError, match="may not contain URLs"):
        BedrockNarrative(
            purpose="This repo provisions resources; see https://attacker.example/login for details.",
        )


def test_bedrock_narrative_rejects_http_url_in_purpose() -> None:
    with pytest.raises(ValidationError, match="may not contain URLs"):
        BedrockNarrative(purpose="Visit http://malicious.test to learn about this repository.")


def test_bedrock_narrative_rejects_url_in_notable_patterns() -> None:
    with pytest.raises(ValidationError, match="may not contain URLs"):
        BedrockNarrative(
            purpose="A repository with multiple notable architectural patterns documented.",
            notable_patterns=["uses workspaces", "links to https://example.com docs"],
        )


def test_bedrock_narrative_accepts_url_free_text() -> None:
    n = BedrockNarrative(
        purpose="A repository with notable architectural patterns documented inline.",
        notable_patterns=["uses workspaces", "manages 3 RDS instances"],
    )
    assert n.notable_patterns == ["uses workspaces", "manages 3 RDS instances"]


# ─── 1.0 API-freeze: renames + aliases + type fixes ──────────────────────


def test_confluence_parent_page_id_ref_is_canonical() -> None:
    """Canonical 1.0 key is parent_page_id_ref; no warning."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg = ConfluenceConfig.model_validate({"parent_page_id_ref": "/custom/path"})
    assert cfg.parent_page_id_ref == "/custom/path"


def test_confluence_deprecated_ssm_path_key_still_works_with_warning() -> None:
    """The pre-1.0 parent_page_id_ssm_path key validates via AliasChoices
    but emits a DeprecationWarning."""
    with pytest.warns(DeprecationWarning, match=r"parent_page_id_ssm_path` is deprecated"):
        cfg = ConfluenceConfig.model_validate({"parent_page_id_ssm_path": "/legacy/path"})
    assert cfg.parent_page_id_ref == "/legacy/path"


def test_llm_optional_string_fields_default_to_none() -> None:
    """vertex_project_id + azure_openai_endpoint use the str|None=None
    sentinel (matches the documented type + openai_organization convention)."""
    cfg = LLMConfig()
    assert cfg.vertex_project_id is None
    assert cfg.azure_openai_endpoint is None
