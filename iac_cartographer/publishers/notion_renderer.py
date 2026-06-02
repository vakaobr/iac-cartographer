"""RepoInventory → Notion block list.

Notion's API takes content as a list of "blocks" — paragraphs, headings,
bulleted lists, tables, etc. Each block is a dict with a `type`
discriminator and a type-specific `<type>` sub-object carrying the
actual content. Rich-text inside a block is a list of `{type: text,
text: {content, link}, annotations: {...}}` runs.

This module renders the inventory to the subset of block types we need:

  * **heading_2** for section headers (Purpose, Resources, Providers, …).
  * **paragraph** for prose (the narrative, last-commit metadata, …).
  * **bulleted_list_item** for top resources + environments lists.
  * **callout** for the banner-SHA marker at the top of every page,
    plus the "AI-H1 prompt-injection" review-queue warning when the
    narrator dropped a narrative.
  * **table** + **table_row** for the providers grid.

Banner SHA storage:

The very first block on every page we publish is a `callout` with the
🔖 emoji and plain text `iac-cartographer SHA: <hex>`. That callout is
the idempotency anchor — the next run reads the first block, parses
the SHA, and short-circuits the rewrite when it matches.

The callout is visible to operators (small, top of page) but
intentionally marked so it's clearly an automation artefact — not
free-form content a human should be editing.

The rendered output here is paired with `iac_cartographer.publishers.notion.NotionPublisher`,
which handles upsert + block-replacement + the find-by-title query.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import RepoInventory

# Banner-SHA marker emoji + text format. Matches what the Confluence /
# Markdown / HTML publishers do — same SHA value, just embedded in a
# different per-publisher carrier (XML comment / HTML comment / callout
# block here).
_SHA_EMOJI = "🔖"
_SHA_PREFIX = "iac-cartographer SHA: "
_SHA_RE = re.compile(rf"{re.escape(_SHA_PREFIX)}([0-9a-f]+)")

# Per-page content caps — Notion limits each block's rich-text content
# to 2000 chars; we truncate aggressively against pathologically int
# narrative outputs to stay well-behaved.
_MAX_BLOCK_CHARS = 1900
_MAX_LIST_ITEMS = 25


def _text(content: str) -> dict[str, Any]:
    """One rich-text run with default annotations."""
    if len(content) > _MAX_BLOCK_CHARS:
        content = content[: _MAX_BLOCK_CHARS - 1] + "…"
    return {"type": "text", "text": {"content": content}}


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [_text(text)]},
    }


def _heading_2(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [_text(text)]},
    }


def _bullet(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_text(text)]},
    }


def _callout(text: str, emoji: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [_text(text)],
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def _sha_callout(sha: str) -> dict[str, Any]:
    """The banner-SHA marker block. Always the first block on every page."""
    return _callout(f"{_SHA_PREFIX}{sha}", _SHA_EMOJI)


def extract_banner_sha(first_block: dict[str, Any] | None) -> str | None:
    """Parse the SHA from the first block of a Notion page.

    `first_block` is what `notion_client.blocks.children.list(page_id, page_size=1)`
    returns — a `callout` if we wrote the page, anything else for pages
    a human created or that pre-date the SHA convention.

    Returns the hex SHA on a match, `None` otherwise — the publisher
    treats `None` as "we don't know the prior SHA, do the write" rather
    than as an error."""
    if not first_block or first_block.get("type") != "callout":
        return None
    rich = first_block.get("callout", {}).get("rich_text", [])
    if not rich:
        return None
    text = rich[0].get("text", {}).get("content", "")
    match = _SHA_RE.search(text)
    return match.group(1) if match else None


def render_child_blocks(
    inv: RepoInventory,
    *,
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None,
) -> list[dict[str, Any]]:
    """Render the deep-dive page for one repo.

    Layout:
      1. Banner-SHA callout (idempotency anchor).
      2. Meta paragraph (last commit / author / updated_at / pipeline link).
      3. Purpose heading + narrative paragraph (or placeholder when the LLM
         dropped this repo's narrative).
      4. Providers heading + bulleted list.
      5. Modules heading + bulleted list (if any).
      6. Top resources heading + bulleted list.
      7. Environments heading + bulleted list (if any).
    """
    blocks: list[dict[str, Any]] = [_sha_callout(sha)]

    # ── meta ─────────────────────────────────────────────────────────
    meta_parts = [
        f"Last commit {inv.meta.last_commit_sha[:8]}",
    ]
    if inv.meta.last_commit_author:
        meta_parts.append(f"by {inv.meta.last_commit_author}")
    meta_parts.append(f"on {inv.meta.last_commit_at.date().isoformat()}")
    meta_parts.append(f"— updated by iac-cartographer at {updated_at.isoformat()}")
    if pipeline_url:
        meta_parts.append(f"(pipeline: {pipeline_url})")
    blocks.append(_paragraph(" ".join(meta_parts)))

    # ── purpose / narrative ──────────────────────────────────────────
    blocks.append(_heading_2("Purpose"))
    if inv.narrative and inv.narrative.purpose:
        blocks.append(_paragraph(inv.narrative.purpose))
        if inv.narrative.environments:
            blocks.append(_paragraph(f"Environments: {', '.join(inv.narrative.environments)}"))
        if inv.narrative.owning_team_guess:
            blocks.append(_paragraph(f"Owning team (guess): {inv.narrative.owning_team_guess}"))
    else:
        blocks.append(
            _callout(
                "Narrative summary unavailable for this run — the LLM "
                "either failed for this repo or the narrator dropped it "
                "(prompt-injection trigger). Structural facts below are "
                "unaffected; auto-retries once per run.",
                "⚠️",
            )
        )

    # ── providers ─────────────────────────────────────────────────────
    if inv.summary.providers:
        blocks.append(_heading_2("Providers"))
        for p in inv.summary.providers[:_MAX_LIST_ITEMS]:
            line = p.name
            if p.source:
                line = f"{p.name} — {p.source}"
            line = f"{line} ({p.version})" if p.version else f"{line} (unpinned)"
            blocks.append(_bullet(line))

    # ── modules ───────────────────────────────────────────────────────
    if inv.summary.modules:
        blocks.append(_heading_2("Modules"))
        for m in inv.summary.modules[:_MAX_LIST_ITEMS]:
            line = f"{m.name} — {m.source}"
            if m.version:
                line = f"{line} ({m.version})"
            blocks.append(_bullet(line))

    # ── top resources ────────────────────────────────────────────────
    if inv.summary.resource_counts_by_type:
        blocks.append(_heading_2("Top resources"))
        top = sorted(inv.summary.resource_counts_by_type.items(), key=lambda kv: -kv[1])[:_MAX_LIST_ITEMS]
        for rtype, count in top:
            blocks.append(_bullet(f"{rtype} — {count}"))

    return blocks


def render_overview_blocks(
    inventories: list[RepoInventory],
    child_page_ids: dict[str, str],
    *,
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None,
) -> list[dict[str, Any]]:
    """Render the overview page.

    Layout:
      1. Banner-SHA callout (idempotency anchor).
      2. Updated-at paragraph + pipeline link.
      3. Summary paragraph (N repos, total resources, top providers).
      4. Heading + bulleted list of every repo (`full_name` → child page link).
    """
    blocks: list[dict[str, Any]] = [_sha_callout(sha)]

    meta_line = f"Updated by iac-cartographer at {updated_at.isoformat()}"
    if pipeline_url:
        meta_line += f" (pipeline: {pipeline_url})"
    blocks.append(_paragraph(meta_line))

    # Aggregate counts (cheap derived facts — operators can scan at the top).
    total_resources = sum(sum(inv.summary.resource_counts_by_type.values()) for inv in inventories)
    provider_counter: dict[str, int] = {}
    for inv in inventories:
        for p in inv.summary.providers:
            provider_counter[p.name] = provider_counter.get(p.name, 0) + 1
    top_providers = sorted(provider_counter.items(), key=lambda kv: -kv[1])[:5]
    top_str = ", ".join(f"{name} (x{n})" for name, n in top_providers) or "—"
    blocks.append(
        _paragraph(
            f"{len(inventories)} repos · {total_resources} resources · top providers: {top_str}",
        )
    )

    blocks.append(_heading_2("Repositories"))
    # Sort alphabetically for a stable, scannable list. Use rich-text
    # link annotations so the bullet text becomes a clickable link to
    # the child page when we have a page ID.
    for full_name in sorted(inv.meta.full_name for inv in inventories):
        child_id = child_page_ids.get(full_name)
        if child_id:
            rich = [
                {
                    "type": "text",
                    "text": {
                        "content": full_name,
                        # Notion accepts a relative URL form for cross-page
                        # links: just the page ID with no dashes.
                        "link": {"url": f"/{child_id.replace('-', '')}"},
                    },
                }
            ]
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": rich},
                }
            )
        else:
            # Repo failed to publish — render the name without a link
            # rather than 404-linking. The orchestrator's outcome
            # reporting covers the failure separately.
            blocks.append(_bullet(full_name))

    return blocks
