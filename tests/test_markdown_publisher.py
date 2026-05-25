"""Tests for the LocalMarkdownPublisher + its pure rendering layer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from iac_cartographer.models import (
    BedrockNarrative,
    ProviderRef,
    RepoInventory,
    RepoMetadata,
    ResourceExplanation,
    ResourceRef,
    TerraformSummary,
    VariableRef,
)
from iac_cartographer.publishers.markdown import LocalMarkdownPublisher
from iac_cartographer.publishers.markdown_renderer import (
    extract_banner_sha,
    render_child_markdown,
    render_overview_markdown,
)


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
                ResourceExplanation(
                    resource_type="grafana_dashboard",
                    why_it_exists="UI overview | with pipe",
                ),
            ],
            environments=["prod", "staging"],
            owning_team_guess="Platform",
            notable_patterns=["one dashboard per service"],
        )
        if with_narrative
        else None
    )
    return RepoInventory(meta=_meta(name), summary=summary, narrative=narrative)


# ─── render_child_markdown ────────────────────────────────────────────────


def test_render_child_includes_banner_and_sections() -> None:
    md = render_child_markdown(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, 10, 0, tzinfo=UTC),
        pipeline_url="https://ci.test/job/42",
    )
    assert "<!-- iac-cartographer-sha: abc12345 -->" in md
    assert "AUTO-GENERATED" in md or "do not edit" in md
    assert "# acme/iac/main-cluster" in md
    assert "## Purpose" in md
    assert "## Providers" in md
    assert "## Inputs" in md
    assert "## Resources by type" in md
    assert "https://ci.test/job/42" in md


def test_render_child_handles_missing_narrative() -> None:
    md = render_child_markdown(
        _inventory(with_narrative=False),
        sha="deadbeef",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert "Narrative summary unavailable" in md
    assert "## Environments" not in md
    assert "## Notable patterns" not in md


def test_render_child_escapes_pipes_in_table_cells() -> None:
    md = render_child_markdown(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    # The "UI overview | with pipe" string should appear escaped in the
    # Key resources table, not as a raw pipe that would break the row.
    assert "UI overview \\| with pipe" in md


# ─── render_overview_markdown ─────────────────────────────────────────────


def test_render_overview_links_to_child_pages() -> None:
    invs = [_inventory("op/a"), _inventory("op/b")]
    child_links = {"op/a": "repos/op__a.md", "op/b": "repos/op__b.md"}
    md = render_overview_markdown(
        invs,
        child_links,
        sha="c0ffee00",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert "[op/a](repos/op__a.md)" in md
    assert "[op/b](repos/op__b.md)" in md
    assert "## At a glance" in md
    assert "2 repositories indexed" in md


def test_render_overview_singular_when_one_repo() -> None:
    invs = [_inventory("solo/repo")]
    md = render_overview_markdown(
        invs,
        {"solo/repo": "repos/solo__repo.md"},
        sha="c0ffee00",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert "1 repository indexed" in md


# ─── extract_banner_sha ───────────────────────────────────────────────────


def test_extract_banner_sha_round_trip() -> None:
    md = render_child_markdown(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert extract_banner_sha(md) == "abc12345"


def test_extract_banner_sha_returns_none_when_absent() -> None:
    assert extract_banner_sha("# no banner here\n\nbody text") is None
    assert extract_banner_sha("") is None


def test_extract_banner_sha_stops_at_first_non_comment() -> None:
    # Banner-like comment further down doesn't count.
    text = "# Heading\n\n<!-- iac-cartographer-sha: abc12345 -->\n"
    assert extract_banner_sha(text) is None


# ─── LocalMarkdownPublisher integration ──────────────────────────────────


@pytest.mark.asyncio
async def test_publisher_writes_child_and_overview(tmp_path: Path) -> None:
    pub = LocalMarkdownPublisher(output_dir=tmp_path)
    inv = _inventory("op/a")
    async with pub as p:
        child_result = await p.publish_child(
            inv,
            sha="abc12345",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )
        overview_result = await p.publish_overview(
            [inv],
            {"op/a": child_result.page_id},
            sha="ffeeddcc",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )

    child_file = tmp_path / "repos" / "op__a.md"
    overview_file = tmp_path / "index.md"
    assert child_file.exists()
    assert overview_file.exists()
    assert child_result.action == "created"
    assert overview_result.action == "created"
    # Overview's link to the child should be a relative path.
    assert "repos/op__a.md" in overview_file.read_text()


@pytest.mark.asyncio
async def test_publisher_short_circuits_on_unchanged_sha(tmp_path: Path) -> None:
    pub = LocalMarkdownPublisher(output_dir=tmp_path)
    inv = _inventory("op/a")
    args = {"sha": "abc12345", "updated_at": datetime(2026, 5, 22, tzinfo=UTC), "pipeline_url": None}
    async with pub as p:
        first = await p.publish_child(inv, **args)  # type: ignore[arg-type]
        second = await p.publish_child(inv, **args)  # type: ignore[arg-type]
    assert first.action == "created"
    assert second.action == "unchanged"


@pytest.mark.asyncio
async def test_publisher_marks_updated_when_sha_differs(tmp_path: Path) -> None:
    pub = LocalMarkdownPublisher(output_dir=tmp_path)
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
    pub = LocalMarkdownPublisher(output_dir=tmp_path)
    inv = _inventory("op/devops/grafana-resources")
    async with pub as p:
        result = await p.publish_child(
            inv,
            sha="abc12345",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )
    expected = tmp_path / "repos" / "op__devops__grafana-resources.md"
    assert expected.exists()
    assert result.page_id == str(expected)
