"""Tests for the Notion block renderer.

Verifies block-list shape (first block = SHA callout), heading +
paragraph + bullet rendering for the inventory subsections,
the placeholder behaviour when narrative is missing, and the
overview's link-annotated bullet list to child pages.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from iac_cartographer.models import (
    BedrockNarrative,
    ModuleRef,
    ProviderRef,
    RepoInventory,
    RepoMetadata,
    TerraformSummary,
)
from iac_cartographer.publishers.notion_renderer import (
    extract_banner_sha,
    render_child_blocks,
    render_overview_blocks,
)


def _meta(full_name: str = "acme-org/main-cluster") -> RepoMetadata:
    return RepoMetadata(
        host="github",
        full_name=full_name,
        clone_url=f"https://github.com/{full_name}.git",
        web_url=f"https://github.com/{full_name}",
        default_branch="main",
        last_commit_sha="abc123ef0000",
        last_commit_at=datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        last_commit_author="alice",
    )


def _summary_basic() -> TerraformSummary:
    return TerraformSummary(
        providers=[
            ProviderRef(name="aws", source="hashicorp/aws", version=">= 5.0"),
            ProviderRef(name="random", source="hashicorp/random", version=None),
        ],
        resource_counts_by_type={"aws_instance": 3, "aws_s3_bucket": 1},
    )


def _narrative() -> BedrockNarrative:
    return BedrockNarrative(
        purpose="Provisions the production VPC plus a small fleet of EC2 instances and one S3 bucket for ingest staging.",
        environments=["prod"],
        owning_team_guess="Platform",
    )


def _inv_with_narrative() -> RepoInventory:
    return RepoInventory(meta=_meta(), summary=_summary_basic(), narrative=_narrative())


def _inv_no_narrative() -> RepoInventory:
    return RepoInventory(meta=_meta(), summary=_summary_basic(), narrative=None)


# ── First-block SHA marker ────────────────────────────────────────────


def test_first_block_is_sha_callout() -> None:
    blocks = render_child_blocks(
        _inv_with_narrative(),
        sha="deadbeef" * 4,
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url=None,
    )
    first = blocks[0]
    assert first["type"] == "callout"
    assert first["callout"]["icon"]["emoji"] == "🔖"
    text = first["callout"]["rich_text"][0]["text"]["content"]
    assert "iac-cartographer SHA: deadbeef" in text


def test_extract_banner_sha_reads_callout() -> None:
    block = {
        "type": "callout",
        "callout": {
            "rich_text": [{"text": {"content": "iac-cartographer SHA: abc123"}}],
        },
    }
    assert extract_banner_sha(block) == "abc123"


def test_extract_banner_sha_returns_none_for_non_callout() -> None:
    block = {"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "hi"}}]}}
    assert extract_banner_sha(block) is None


def test_extract_banner_sha_returns_none_for_empty_first_block() -> None:
    assert extract_banner_sha(None) is None
    assert extract_banner_sha({}) is None


def test_extract_banner_sha_returns_none_when_marker_missing() -> None:
    """A callout that doesn't carry our SHA prefix returns None — the
    publisher treats that as "unknown prior SHA, do the write" rather
    than as an error."""
    block = {
        "type": "callout",
        "callout": {
            "rich_text": [{"text": {"content": "Just a human-written callout"}}],
        },
    }
    assert extract_banner_sha(block) is None


# ── Child blocks ──────────────────────────────────────────────────────


def test_child_includes_purpose_heading_and_narrative_paragraph() -> None:
    blocks = render_child_blocks(
        _inv_with_narrative(),
        sha="x",
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url=None,
    )
    headings = [b for b in blocks if b["type"] == "heading_2"]
    heading_texts = [h["heading_2"]["rich_text"][0]["text"]["content"] for h in headings]
    assert "Purpose" in heading_texts
    # The narrative paragraph follows the Purpose heading.
    purpose_idx = next(
        i
        for i, b in enumerate(blocks)
        if b.get("type") == "heading_2" and b["heading_2"]["rich_text"][0]["text"]["content"] == "Purpose"
    )
    narrative_block = blocks[purpose_idx + 1]
    assert narrative_block["type"] == "paragraph"
    assert "production VPC" in narrative_block["paragraph"]["rich_text"][0]["text"]["content"]


def test_child_renders_providers_with_unpinned_marker() -> None:
    blocks = render_child_blocks(
        _inv_with_narrative(),
        sha="x",
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url=None,
    )
    bullets = [b for b in blocks if b["type"] == "bulleted_list_item"]
    texts = [b["bulleted_list_item"]["rich_text"][0]["text"]["content"] for b in bullets]
    assert any("aws" in t and ">= 5.0" in t for t in texts)
    # Unpinned providers get the `(unpinned)` marker.
    assert any("random" in t and "(unpinned)" in t for t in texts)


def test_child_top_resources_sorted_by_count_desc() -> None:
    blocks = render_child_blocks(
        _inv_with_narrative(),
        sha="x",
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url=None,
    )
    # Find the "Top resources" heading + the bullets that follow.
    idx = next(
        i
        for i, b in enumerate(blocks)
        if b.get("type") == "heading_2" and b["heading_2"]["rich_text"][0]["text"]["content"] == "Top resources"
    )
    bullets_after = [b for b in blocks[idx + 1 :] if b["type"] == "bulleted_list_item"]
    texts = [b["bulleted_list_item"]["rich_text"][0]["text"]["content"] for b in bullets_after]
    # aws_instance (3) should come before aws_s3_bucket (1).
    instance_idx = next(i for i, t in enumerate(texts) if "aws_instance" in t)
    bucket_idx = next(i for i, t in enumerate(texts) if "aws_s3_bucket" in t)
    assert instance_idx < bucket_idx


def test_child_falls_back_to_warning_callout_when_narrative_missing() -> None:
    """The narrator drops repos with prompt-injection triggers — the page
    still renders structural facts but the Purpose section is a clear
    warning callout, not a generic empty paragraph."""
    blocks = render_child_blocks(
        _inv_no_narrative(),
        sha="x",
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url=None,
    )
    purpose_idx = next(
        i
        for i, b in enumerate(blocks)
        if b.get("type") == "heading_2" and b["heading_2"]["rich_text"][0]["text"]["content"] == "Purpose"
    )
    warning_block = blocks[purpose_idx + 1]
    assert warning_block["type"] == "callout"
    assert warning_block["callout"]["icon"]["emoji"] == "⚠️"


def test_child_meta_paragraph_carries_pipeline_url_when_set() -> None:
    blocks = render_child_blocks(
        _inv_with_narrative(),
        sha="x",
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url="https://ci.example.com/run/42",
    )
    # The meta paragraph is the second block (after the SHA callout).
    meta = blocks[1]
    assert meta["type"] == "paragraph"
    text = meta["paragraph"]["rich_text"][0]["text"]["content"]
    assert "alice" in text
    assert "ci.example.com/run/42" in text


# ── Overview blocks ───────────────────────────────────────────────────


def test_overview_carries_repo_count_and_top_providers() -> None:
    inv = _inv_with_narrative()
    blocks = render_overview_blocks(
        [inv],
        {inv.meta.full_name: "abc-page-uuid-1"},
        sha="y",
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url=None,
    )
    # First block is the SHA callout, second the updated-at line,
    # third the summary paragraph.
    summary_text = blocks[2]["paragraph"]["rich_text"][0]["text"]["content"]
    assert "1 repos" in summary_text
    # Top resources count comes from resource_counts_by_type sum (3 + 1).
    assert "4 resources" in summary_text
    # Top providers list mentions aws.
    assert "aws" in summary_text


def test_overview_repositories_list_links_to_child_pages() -> None:
    inv = _inv_with_narrative()
    blocks = render_overview_blocks(
        [inv],
        {inv.meta.full_name: "12345678-abcd-efgh-ijkl-mnopqrstuvwx"},
        sha="y",
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url=None,
    )
    bullets = [b for b in blocks if b["type"] == "bulleted_list_item"]
    assert len(bullets) == 1
    rich = bullets[0]["bulleted_list_item"]["rich_text"][0]
    assert rich["text"]["content"] == inv.meta.full_name
    # Link target uses the page UUID with dashes stripped — Notion's
    # relative-URL convention.
    assert rich["text"]["link"]["url"] == "/12345678abcdefghijklmnopqrstuvwx"


def test_overview_falls_back_to_plain_bullet_when_no_child_id() -> None:
    """A repo that failed to publish has no child page ID — render the
    name without a link rather than emitting a broken URL."""
    inv = _inv_with_narrative()
    blocks = render_overview_blocks(
        [inv],
        child_page_ids={},  # repo failed to publish — no ID
        sha="y",
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url=None,
    )
    bullets = [b for b in blocks if b["type"] == "bulleted_list_item"]
    rich = bullets[0]["bulleted_list_item"]["rich_text"][0]
    # No `link` annotation when there's no child page.
    assert "link" not in rich.get("text", {})
    assert rich["text"]["content"] == inv.meta.full_name


def test_overview_bullets_alphabetically_sorted() -> None:
    inv_z = RepoInventory(meta=_meta("z-org/last"), summary=_summary_basic(), narrative=None)
    inv_a = RepoInventory(meta=_meta("a-org/first"), summary=_summary_basic(), narrative=None)
    blocks = render_overview_blocks(
        [inv_z, inv_a],
        child_page_ids={},
        sha="y",
        updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        pipeline_url=None,
    )
    bullets = [b for b in blocks if b["type"] == "bulleted_list_item"]
    names = [b["bulleted_list_item"]["rich_text"][0]["text"]["content"] for b in bullets]
    assert names == ["a-org/first", "z-org/last"]


# ── Truncation ────────────────────────────────────────────────────────


def test_text_helper_truncates_content_above_1900_chars() -> None:
    """Notion caps each block's text at 2000 chars; the renderer
    truncates aggressively at 1900 so the API doesn't reject
    pathologically int content. Test the helper directly — no
    BedrockNarrative field allows > 1900 chars (purpose is capped
    at 600 by the model), so a render-path test couldn't reach
    this branch."""
    from iac_cartographer.publishers.notion_renderer import _text

    long_content = "x" * 4000
    run = _text(long_content)
    assert run["type"] == "text"
    truncated = run["text"]["content"]
    assert len(truncated) == 1900
    assert truncated.endswith("…")


def test_text_helper_passes_short_content_through_unchanged() -> None:
    """Below the cap, `_text` is a straight pass-through. Pinning
    the non-truncation branch so a future change to the threshold
    fails loudly."""
    from iac_cartographer.publishers.notion_renderer import _text

    run = _text("short and sweet")
    assert run["text"]["content"] == "short and sweet"
    assert not run["text"]["content"].endswith("…")


# ── modules section (covers an inv with non-empty modules list) ───────


def test_render_child_blocks_renders_modules_section_when_modules_present() -> None:
    """Inventories with a non-empty `modules` list emit a "Modules"
    heading + one bullet per module. The bullet text reads
    `<name> — <source>` for unpinned modules and
    `<name> — <source> (<version>)` for pinned ones."""
    summary = TerraformSummary(
        providers=[ProviderRef(name="aws", source="hashicorp/aws", version=">= 5.0")],
        modules=[
            ModuleRef(name="vpc", source="terraform-aws-modules/vpc/aws", version="5.0.0"),
            ModuleRef(name="utils", source="git::https://github.com/acme/utils.git", version=None),
        ],
        resource_counts_by_type={"aws_instance": 1},
    )
    inv = RepoInventory(meta=_meta(), summary=summary, narrative=None)
    blocks = render_child_blocks(inv, sha="m", updated_at=datetime(2026, 5, 26, 9, 0, tzinfo=UTC), pipeline_url=None)

    # Find the "Modules" heading and the bullets that follow until the
    # next heading.
    headings = [(i, b) for i, b in enumerate(blocks) if b.get("type") == "heading_2"]
    modules_heading_idx = next(i for i, b in headings if b["heading_2"]["rich_text"][0]["text"]["content"] == "Modules")
    # Bullets follow the heading until the next heading_2.
    next_heading = next((i for i, b in headings if i > modules_heading_idx), len(blocks))
    bullets = blocks[modules_heading_idx + 1 : next_heading]
    bullet_texts = [b["bulleted_list_item"]["rich_text"][0]["text"]["content"] for b in bullets]

    assert "vpc — terraform-aws-modules/vpc/aws (5.0.0)" in bullet_texts
    assert "utils — git::https://github.com/acme/utils.git" in bullet_texts


# ── extract_banner_sha edge: callout with empty rich_text ────────────


def test_extract_banner_sha_returns_none_for_callout_with_empty_rich_text() -> None:
    """An existing first block of type `callout` but with no
    rich_text entries (someone hand-cleared the SHA but left the
    block shell) must not crash — return None so the publisher falls
    through to the "update" path on the next run."""
    block_with_empty_rich = {"type": "callout", "callout": {"rich_text": []}}
    assert extract_banner_sha(block_with_empty_rich) is None


# Sanity check that pytest-asyncio's auto-mode doesn't bleed into these
# pure-sync tests.
def test_module_is_pure_sync() -> None:
    """No `async def` in the renderer — composing block dicts is
    synchronous. Just affirms the file's intent."""
    import inspect

    from iac_cartographer.publishers import notion_renderer

    for name in dir(notion_renderer):
        attr = getattr(notion_renderer, name)
        if callable(attr) and not name.startswith("_"):
            assert not inspect.iscoroutinefunction(attr), f"{name} unexpectedly async"


pytest.mark.asyncio_mode = "auto"  # documents the conftest default
