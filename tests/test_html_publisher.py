"""Tests for the LocalHtmlPublisher + its pure rendering layer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

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
from iac_cartographer.publishers.html import LocalHtmlPublisher
from iac_cartographer.publishers.html_renderer import (
    extract_banner_sha,
    render_child_html,
    render_overview_html,
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
                ResourceExplanation(
                    resource_type="grafana_dashboard",
                    why_it_exists="UI overview with <script>alert(1)</script>",
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


# ─── render_child_html ────────────────────────────────────────────────────


def test_render_child_includes_html_scaffold_and_sections() -> None:
    html = render_child_html(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, 10, 0, tzinfo=UTC),
        pipeline_url="https://ci.test/job/42",
    )
    assert html.startswith("<!DOCTYPE html>")
    assert '<meta name="iac-cartographer-sha" content="abc12345">' in html
    assert "<title>acme/iac/main-cluster — iac-cartographer</title>" in html
    assert "<h1>acme/iac/main-cluster</h1>" in html
    assert "<h2>Purpose</h2>" in html
    assert "<h2>Providers</h2>" in html
    assert "<h2>Inputs</h2>" in html
    assert "<h2>Resources by type</h2>" in html
    assert "https://ci.test/job/42" in html
    # Embedded CSS, no external link.
    assert "<style>" in html
    assert "<link " not in html


def test_render_child_escapes_html_in_narrative() -> None:
    """The narrative `why_it_exists` deliberately contains `<script>`. The
    HTML renderer MUST escape it so an indirect prompt-injection attack
    can't smuggle script tags into the published page."""
    html = render_child_html(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    # Raw script tag must NOT appear; escaped form must.
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_render_child_handles_missing_narrative() -> None:
    html = render_child_html(
        _inventory(with_narrative=False),
        sha="deadbeef",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert "Narrative summary unavailable" in html
    assert "<h2>Environments</h2>" not in html
    assert "<h2>Notable patterns</h2>" not in html


def test_render_child_alias_empty_cell_uses_literal_html() -> None:
    """The provider table's "no alias" fallback embeds a literal `<span
    class="muted">—</span>`. Earlier draft had `&quot;`-escaped quotes
    inside the attribute, which browsers tolerate but break the class
    binding. Regression check: the actual attribute literal appears."""
    html = render_child_html(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert '<span class="muted">—</span>' in html
    assert "class=&quot;muted&quot;" not in html


def test_render_child_no_javascript_when_no_resource_graph() -> None:
    """No JS in the output when the resource graph is absent — preserves
    the publisher's `file://`, PDF-export, and locked-down-browser
    properties for any repo without resources (terraform-doc-only
    pages, deprecated repos, etc.)."""
    from iac_cartographer.models import RepoInventory, TerraformSummary

    inv = _inventory()
    empty = RepoInventory(
        meta=inv.meta,
        summary=TerraformSummary(),  # no resources → no Mermaid graph
        narrative=inv.narrative,
    )
    html = render_child_html(
        empty,
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert "<script" not in html
    assert "javascript:" not in html
    assert "onclick=" not in html


def test_render_child_only_emits_mermaid_script_when_resource_graph_present() -> None:
    """When a resource graph IS emitted, the ONLY script in the document
    is the pinned Mermaid CDN bundle that renders it. No inline event
    handlers, no `javascript:` URLs, no other script tags — the file
    is still safe for audit/PDF workflows (just disable JS to skip
    diagram rendering) and the dependency surface is one URL."""
    html = render_child_html(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert html.count("<script") == 2  # CDN tag + DOMContentLoaded initialiser
    assert "cdn.jsdelivr.net/npm/mermaid" in html
    assert "javascript:" not in html
    assert "onclick=" not in html


# ─── render_overview_html ─────────────────────────────────────────────────


def test_render_overview_links_to_child_pages() -> None:
    invs = [_inventory("op/a"), _inventory("op/b")]
    child_links = {"op/a": "repos/op__a.html", "op/b": "repos/op__b.html"}
    html = render_overview_html(
        invs,
        child_links,
        sha="c0ffee00",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert '<a href="repos/op__a.html">op/a</a>' in html
    assert '<a href="repos/op__b.html">op/b</a>' in html
    assert "<h2>Inventory</h2>" in html
    assert "<h2>At a glance</h2>" in html
    assert "2 repositories indexed" in html


def test_render_overview_singular_when_one_repo() -> None:
    html = render_overview_html(
        [_inventory("solo/repo")],
        {"solo/repo": "repos/solo__repo.html"},
        sha="c0ffee00",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert "1 repository indexed" in html


# ─── extract_banner_sha ───────────────────────────────────────────────────


def test_extract_banner_sha_round_trip() -> None:
    html = render_child_html(
        _inventory(),
        sha="abc12345",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        pipeline_url=None,
    )
    assert extract_banner_sha(html) == "abc12345"


def test_extract_banner_sha_returns_none_when_absent() -> None:
    assert extract_banner_sha("<!DOCTYPE html><html><body>no banner</body></html>") is None
    assert extract_banner_sha("") is None


def test_extract_banner_sha_only_scans_head() -> None:
    # A meta-shaped string in the body must not be picked up. The reader
    # only scans the first ~1 KB to keep the per-publish cost bounded.
    head_filler = "x" * 1500
    text = (
        f"<!DOCTYPE html><html><head>{head_filler}</head>"
        f'<body><div>name="iac-cartographer-sha" content="abc12345"</div></body></html>'
    )
    assert extract_banner_sha(text) is None


# ─── LocalHtmlPublisher integration ──────────────────────────────────────


@pytest.mark.asyncio
async def test_publisher_writes_child_and_overview(tmp_path: Path) -> None:
    pub = LocalHtmlPublisher(output_dir=tmp_path)
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

    child_file = tmp_path / "repos" / "op__a.html"
    overview_file = tmp_path / "index.html"
    assert child_file.exists()
    assert overview_file.exists()
    assert child_result.action == "created"
    assert overview_result.action == "created"
    # Overview's link to the child is a relative path (works under file://
    # AND when uploaded to S3 / GitHub Pages).
    assert 'href="repos/op__a.html"' in overview_file.read_text()


@pytest.mark.asyncio
async def test_publisher_short_circuits_on_unchanged_sha(tmp_path: Path) -> None:
    pub = LocalHtmlPublisher(output_dir=tmp_path)
    inv = _inventory("op/a")
    args = {"sha": "abc12345", "updated_at": datetime(2026, 5, 22, tzinfo=UTC), "pipeline_url": None}
    async with pub as p:
        first = await p.publish_child(inv, **args)  # type: ignore[arg-type]
        second = await p.publish_child(inv, **args)  # type: ignore[arg-type]
    assert first.action == "created"
    assert second.action == "unchanged"


@pytest.mark.asyncio
async def test_publisher_marks_updated_when_sha_differs(tmp_path: Path) -> None:
    pub = LocalHtmlPublisher(output_dir=tmp_path)
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
    pub = LocalHtmlPublisher(output_dir=tmp_path)
    inv = _inventory("op/devops/grafana-resources")
    async with pub as p:
        result = await p.publish_child(
            inv,
            sha="abc12345",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )
    expected = tmp_path / "repos" / "op__devops__grafana-resources.html"
    assert expected.exists()
    assert result.page_id == str(expected)
