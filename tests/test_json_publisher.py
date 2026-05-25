"""Tests for the LocalJsonPublisher + its pure rendering layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import yaml

from iac_cartographer.models import (
    AppConfig,
    BedrockNarrative,
    ProviderRef,
    RepoInventory,
    RepoMetadata,
    ResourceExplanation,
    ResourceRef,
    TerraformSummary,
    VariableRef,
)
from iac_cartographer.publishers.json_publisher import LocalJsonPublisher
from iac_cartographer.publishers.json_renderer import (
    SCHEMA_VERSION,
    extract_banner_sha,
    render_child_json,
    render_overview_json,
)

if TYPE_CHECKING:
    from pathlib import Path


def _meta(name: str = "acme/iac/main-cluster") -> RepoMetadata:
    return RepoMetadata(
        host="gitlab",
        full_name=name,
        clone_url=f"https://x.test/{name}.git",
        web_url=f"https://x.test/{name}",
        default_branch="main",
        last_commit_sha="a" * 40,
        last_commit_at=datetime(2026, 5, 22, tzinfo=UTC),
        last_commit_author="Alice <alice@example.com>",
    )


def _inventory(name: str = "acme/iac/main-cluster", with_narrative: bool = True) -> RepoInventory:
    summary = TerraformSummary(
        providers=[ProviderRef(name="aws", source="hashicorp/aws", version=">= 6.0")],
        resources=[
            ResourceRef(type="aws_iam_role", name="task"),
            ResourceRef(type="grafana_dashboard", name="overview"),
        ],
        inputs=[VariableRef(name="region", type="string", required=True, description="AWS region")],
        resource_counts_by_type={"aws_iam_role": 1, "grafana_dashboard": 1},
    )
    narrative = (
        BedrockNarrative(
            purpose="Provisions Grafana dashboards and IAM roles for observability.",
            key_resources_explained=[
                ResourceExplanation(resource_type="grafana_dashboard", why_it_exists="UI overview"),
            ],
            environments=["prod", "staging"],
            owning_team_guess="Platform",
            notable_patterns=["one dashboard per service"],
        )
        if with_narrative
        else None
    )
    return RepoInventory(meta=_meta(name), summary=summary, narrative=narrative)


# ─── render_child_json ────────────────────────────────────────────────────


def test_render_child_is_valid_json_with_expected_top_level_keys() -> None:
    text = render_child_json(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, 10, 0, tzinfo=UTC),
        pipeline_url="https://ci.test/job/42",
    )
    payload = json.loads(text)
    assert set(payload.keys()) == {"iac_cartographer", "meta", "summary", "narrative"}
    banner = payload["iac_cartographer"]
    assert banner["sha"] == "abc12345"
    assert banner["schema_version"] == SCHEMA_VERSION
    assert banner["generator"] == "iac-cartographer"
    assert banner["pipeline_url"] == "https://ci.test/job/42"


def test_render_child_serialises_meta_summary_narrative_round_trip() -> None:
    inv = _inventory()
    text = render_child_json(
        inv,
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    payload = json.loads(text)
    assert payload["meta"]["full_name"] == "acme/iac/main-cluster"
    assert payload["meta"]["host"] == "gitlab"
    assert payload["summary"]["resource_counts_by_type"] == {
        "aws_iam_role": 1,
        "grafana_dashboard": 1,
    }
    assert payload["narrative"]["purpose"].startswith("Provisions Grafana")
    # The raw last_commit_at gets serialised as an ISO-8601 string.
    assert payload["meta"]["last_commit_at"].startswith("2026-05-22T")


def test_render_child_narrative_is_null_when_absent() -> None:
    text = render_child_json(
        _inventory(with_narrative=False),
        sha="deadbeef",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    payload = json.loads(text)
    assert payload["narrative"] is None


def test_render_child_omits_pipeline_url_when_none() -> None:
    text = render_child_json(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    payload = json.loads(text)
    assert "pipeline_url" not in payload["iac_cartographer"]


# ─── render_overview_json ─────────────────────────────────────────────────


def test_render_overview_has_aggregates_and_repos() -> None:
    invs = [_inventory("op/a"), _inventory("op/b")]
    child_links = {"op/a": "repos/op__a.json", "op/b": "repos/op__b.json"}
    text = render_overview_json(
        invs,
        child_links,
        sha="c0ffee00",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    payload = json.loads(text)
    assert set(payload.keys()) == {"iac_cartographer", "aggregates", "repos"}
    assert payload["aggregates"]["repo_count"] == 2
    # 2 resources per repo x 2 repos.
    assert payload["aggregates"]["total_resources"] == 4
    assert payload["aggregates"]["top_providers"] == [{"name": "aws", "repo_count": 2}]
    names = [r["full_name"] for r in payload["repos"]]
    assert sorted(names) == ["op/a", "op/b"]


def test_render_overview_includes_child_document_pointer() -> None:
    invs = [_inventory("op/a")]
    text = render_overview_json(
        invs,
        {"op/a": "repos/op__a.json"},
        sha="c0ffee00",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    payload = json.loads(text)
    assert payload["repos"][0]["child_document"] == "repos/op__a.json"


# ─── extract_banner_sha ───────────────────────────────────────────────────


def test_extract_banner_sha_round_trip() -> None:
    text = render_child_json(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert extract_banner_sha(text) == "abc12345"


def test_extract_banner_sha_returns_none_when_absent() -> None:
    assert extract_banner_sha("{}") is None
    assert extract_banner_sha("") is None
    assert extract_banner_sha("not valid json{") is None


def test_extract_banner_sha_ignores_top_level_array() -> None:
    # A SHA-shaped string in an array element must not be picked up.
    assert extract_banner_sha('["abc12345"]') is None


# ─── LocalJsonPublisher integration ──────────────────────────────────────


@pytest.mark.asyncio
async def test_publisher_writes_child_and_overview(tmp_path: Path) -> None:
    pub = LocalJsonPublisher(output_dir=tmp_path)
    inv = _inventory("op/a")
    async with pub as p:
        child_result = await p.publish_child(
            inv,
            sha="abc12345",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )
        await p.publish_overview(
            [inv],
            {"op/a": child_result.page_id},
            sha="ffeeddcc",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )

    child_file = tmp_path / "repos" / "op__a.json"
    overview_file = tmp_path / "index.json"
    assert child_file.exists()
    assert overview_file.exists()

    overview = json.loads(overview_file.read_text())
    # The overview's child_document pointer is a path relative to the
    # output_dir (not an absolute path from the filesystem).
    assert overview["repos"][0]["child_document"] == "repos/op__a.json"


@pytest.mark.asyncio
async def test_publisher_short_circuits_on_unchanged_sha(tmp_path: Path) -> None:
    pub = LocalJsonPublisher(output_dir=tmp_path)
    inv = _inventory("op/a")
    args = {"sha": "abc12345", "updated_at": datetime(2026, 5, 22, tzinfo=UTC), "pipeline_url": None}
    async with pub as p:
        first = await p.publish_child(inv, **args)  # type: ignore[arg-type]
        second = await p.publish_child(inv, **args)  # type: ignore[arg-type]
    assert first.action == "created"
    assert second.action == "unchanged"


@pytest.mark.asyncio
async def test_publisher_marks_updated_when_sha_differs(tmp_path: Path) -> None:
    pub = LocalJsonPublisher(output_dir=tmp_path)
    inv = _inventory("op/a")
    async with pub as p:
        first = await p.publish_child(
            inv,
            sha="abc12345",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )
        second = await p.publish_child(
            inv,
            sha="newshaaa",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )
    assert first.action == "created"
    assert second.action == "updated"


@pytest.mark.asyncio
async def test_publisher_slugs_slashes_in_repo_name(tmp_path: Path) -> None:
    pub = LocalJsonPublisher(output_dir=tmp_path)
    inv = _inventory("op/devops/grafana-resources")
    async with pub as p:
        result = await p.publish_child(
            inv,
            sha="abc12345",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )
    expected = tmp_path / "repos" / "op__devops__grafana-resources.json"
    assert expected.exists()
    assert result.page_id == str(expected)


# ─── AppConfig wiring ─────────────────────────────────────────────────────


def test_app_config_accepts_json_yaml_key() -> None:
    """YAML uses `json:` (the alias); Python uses `json_output` (the
    field name). Both must resolve to the same `JsonConfig`."""
    parsed = yaml.safe_load("publisher:\n  kind: json\njson:\n  output_dir: ./out\n")
    cfg = AppConfig.model_validate(parsed)
    assert cfg.publisher.kind == "json"
    assert cfg.json_output.output_dir == "./out"
