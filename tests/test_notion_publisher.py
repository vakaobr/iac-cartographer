"""Tests for the Notion publisher.

The notion-client SDK exposes a `pages` + `blocks` API surface; we mock
the relevant methods via `unittest.mock.AsyncMock` so the tests don't
need a live Notion workspace. Each test asserts on the calls the
publisher makes (create / list / append / delete) and on the
PublishResult action returned to the orchestrator.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iac_cartographer.models import (
    NotionCredentials,
    ProviderRef,
    RepoInventory,
    RepoMetadata,
    TerraformSummary,
)
from iac_cartographer.publishers.base import PublishResult
from iac_cartographer.publishers.notion import NotionPublisher

PARENT_ID = "11111111-1111-1111-1111-111111111111"
CHILD_ID = "22222222-2222-2222-2222-222222222222"


def _inv(full_name: str = "acme/main") -> RepoInventory:
    return RepoInventory(
        meta=RepoMetadata(
            host="github",
            full_name=full_name,
            clone_url=f"https://github.com/{full_name}.git",
            web_url=f"https://github.com/{full_name}",
            default_branch="main",
            last_commit_sha="abc123",
            last_commit_at=datetime(2026, 1, 15, tzinfo=UTC),
            last_commit_author="alice",
        ),
        summary=TerraformSummary(
            providers=[ProviderRef(name="aws", source="hashicorp/aws", version=">= 5.0")],
            resource_counts_by_type={"aws_instance": 1},
        ),
        narrative=None,
    )


def _mk_client() -> MagicMock:
    """Build a MagicMock shaped like `notion_client.AsyncClient`. The
    nested attributes (`.blocks.children.list`, `.pages.create`, …) are
    AsyncMock so awaiting them works without an extra wrapper."""
    client = MagicMock()
    client.aclose = AsyncMock()
    client.pages.create = AsyncMock()
    client.blocks.children.list = AsyncMock()
    client.blocks.children.append = AsyncMock()
    client.blocks.delete = AsyncMock()
    return client


async def _publisher_with_client(client: MagicMock) -> NotionPublisher:
    """Build a NotionPublisher with the notion-client construction
    short-circuited to return our mock. Returns it AFTER `__aenter__`
    so tests can call `publish_child` / `publish_overview` directly."""
    creds = NotionCredentials(integration_token="secret_test")
    pub = NotionPublisher(creds, parent_page_id=PARENT_ID)
    # Patch the lazy-import factory so `__aenter__` installs our mock.
    pub._make_client = lambda: client  # type: ignore[method-assign]
    await pub.__aenter__()
    return pub


# ── create path (no existing child) ───────────────────────────────────


async def test_publish_child_creates_new_sub_page_when_none_exists() -> None:
    client = _mk_client()
    # find_child_by_title walks children and finds nothing.
    client.blocks.children.list.return_value = {"results": [], "has_more": False}
    client.pages.create.return_value = {"id": CHILD_ID}

    pub = await _publisher_with_client(client)
    result = await pub.publish_child(
        _inv(), sha="abc123", updated_at=datetime(2026, 5, 26, tzinfo=UTC), pipeline_url=None
    )

    assert isinstance(result, PublishResult)
    assert result.page_id == CHILD_ID
    assert result.action == "created"
    # `pages.create` was called with the parent UUID + title set to the repo's full_name.
    create_kwargs = client.pages.create.call_args.kwargs
    assert create_kwargs["parent"]["page_id"] == PARENT_ID
    assert create_kwargs["properties"]["title"]["title"][0]["text"]["content"] == "acme/main"
    # First block in the create payload is the SHA callout.
    first_block = create_kwargs["children"][0]
    assert first_block["type"] == "callout"
    assert "iac-cartographer SHA: abc123" in first_block["callout"]["rich_text"][0]["text"]["content"]


# ── unchanged path (existing child with matching SHA) ─────────────────


async def test_publish_child_short_circuits_when_sha_matches() -> None:
    client = _mk_client()
    # First list call: parent's children, returning one child_page that matches.
    # Second list call: that page's first block, returning our SHA callout.
    client.blocks.children.list.side_effect = [
        {
            "results": [
                {"id": CHILD_ID, "type": "child_page", "child_page": {"title": "acme/main"}},
            ],
            "has_more": False,
        },
        {
            "results": [
                {
                    "type": "callout",
                    "callout": {"rich_text": [{"text": {"content": "iac-cartographer SHA: abc123"}}]},
                }
            ],
        },
    ]

    pub = await _publisher_with_client(client)
    result = await pub.publish_child(
        _inv(), sha="abc123", updated_at=datetime(2026, 5, 26, tzinfo=UTC), pipeline_url=None
    )

    assert result.action == "unchanged"
    assert result.page_id == CHILD_ID
    # No write happened.
    client.pages.create.assert_not_awaited()
    client.blocks.children.append.assert_not_awaited()
    client.blocks.delete.assert_not_awaited()


# ── update path (existing child, SHA changed) ─────────────────────────


async def test_publish_child_replaces_blocks_when_sha_differs() -> None:
    client = _mk_client()
    # find_child returns an existing match.
    # read_sha returns the OLD sha.
    # _replace_blocks lists children to get IDs (returns 2 blocks), then deletes them, then appends new ones.
    client.blocks.children.list.side_effect = [
        # parent.children.list
        {
            "results": [
                {"id": CHILD_ID, "type": "child_page", "child_page": {"title": "acme/main"}},
            ],
            "has_more": False,
        },
        # page first block — read_sha
        {
            "results": [
                {
                    "type": "callout",
                    "callout": {"rich_text": [{"text": {"content": "iac-cartographer SHA: old"}}]},
                }
            ],
        },
        # _replace_blocks: list existing children to archive
        {
            "results": [
                {"id": "block-a"},
                {"id": "block-b"},
            ],
            "has_more": False,
        },
    ]

    pub = await _publisher_with_client(client)
    result = await pub.publish_child(
        _inv(),
        sha="new",
        updated_at=datetime(2026, 5, 26, tzinfo=UTC),
        pipeline_url=None,
    )

    assert result.action == "updated"
    assert result.page_id == CHILD_ID
    # Both existing blocks were deleted.
    assert client.blocks.delete.await_count == 2
    delete_ids = {call.kwargs["block_id"] for call in client.blocks.delete.await_args_list}
    assert delete_ids == {"block-a", "block-b"}
    # New blocks appended (the first new one has the new SHA).
    assert client.blocks.children.append.await_count == 1
    appended = client.blocks.children.append.await_args.kwargs["children"]
    assert appended[0]["type"] == "callout"
    assert "iac-cartographer SHA: new" in appended[0]["callout"]["rich_text"][0]["text"]["content"]


# ── overview path ─────────────────────────────────────────────────────


async def test_publish_overview_creates_overview_sub_page_when_none_exists() -> None:
    client = _mk_client()
    client.blocks.children.list.return_value = {"results": [], "has_more": False}
    client.pages.create.return_value = {"id": "overview-id"}

    pub = await _publisher_with_client(client)
    inv = _inv()
    result = await pub.publish_overview(
        [inv],
        {inv.meta.full_name: CHILD_ID},
        sha="sha-overview",
        updated_at=datetime(2026, 5, 26, tzinfo=UTC),
        pipeline_url="https://ci.example.com/42",
    )

    assert result.action == "created"
    assert result.page_id == "overview-id"
    create_kwargs = client.pages.create.call_args.kwargs
    assert create_kwargs["properties"]["title"]["title"][0]["text"]["content"] == "Overview"


# ── pagination ────────────────────────────────────────────────────────


async def test_find_child_walks_pagination_until_match_or_exhaust() -> None:
    """Notion paginates children at 100/page. The publisher follows
    `next_cursor` until it finds the title or runs out of pages."""
    client = _mk_client()
    # Page 1: no match. Page 2: match.
    client.blocks.children.list.side_effect = [
        {
            "results": [
                {"id": "x1", "type": "child_page", "child_page": {"title": "other-1"}},
                {"id": "x2", "type": "child_page", "child_page": {"title": "other-2"}},
            ],
            "has_more": True,
            "next_cursor": "cursor-1",
        },
        {
            "results": [
                {"id": CHILD_ID, "type": "child_page", "child_page": {"title": "acme/main"}},
            ],
            "has_more": False,
        },
        # SHA read returns matching sha → unchanged short-circuit.
        {
            "results": [
                {
                    "type": "callout",
                    "callout": {"rich_text": [{"text": {"content": "iac-cartographer SHA: abc123"}}]},
                }
            ],
        },
    ]

    pub = await _publisher_with_client(client)
    result = await pub.publish_child(
        _inv(), sha="abc123", updated_at=datetime(2026, 5, 26, tzinfo=UTC), pipeline_url=None
    )

    assert result.action == "unchanged"
    # Confirmed the publisher followed the cursor on call 2.
    list_calls = client.blocks.children.list.await_args_list
    assert list_calls[0].kwargs.get("start_cursor") is None
    assert list_calls[1].kwargs["start_cursor"] == "cursor-1"


# ── chunked append ────────────────────────────────────────────────────


async def test_create_chunks_blocks_above_appendable_cap() -> None:
    """Notion's `children` field on create + append caps at 100; we
    chunk at 90 to leave headroom. Verify with an artificially large
    block list."""
    client = _mk_client()
    client.blocks.children.list.return_value = {"results": [], "has_more": False}
    client.pages.create.return_value = {"id": CHILD_ID}

    pub = await _publisher_with_client(client)

    # Inject 200 fake blocks via a patched renderer call.
    big_blocks = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}} for _ in range(200)]
    with patch("iac_cartographer.publishers.notion.render_child_blocks", return_value=big_blocks):
        await pub.publish_child(_inv(), sha="x", updated_at=datetime(2026, 5, 26, tzinfo=UTC), pipeline_url=None)

    # `pages.create` receives the first 90 blocks; the remaining 110
    # land via two `blocks.children.append` calls (90 + 20).
    create_kwargs = client.pages.create.call_args.kwargs
    assert len(create_kwargs["children"]) == 90
    assert client.blocks.children.append.await_count == 2
    append_lens = [len(call.kwargs["children"]) for call in client.blocks.children.append.await_args_list]
    assert append_lens == [90, 20]


# ── lazy-import error ────────────────────────────────────────────────


async def test_aenter_raises_when_notion_client_missing() -> None:
    """If notion-client isn't installed, the lazy-import path raises
    with a clear pip-install hint when the publisher is entered."""
    from iac_cartographer.publishers.notion import _NotionImportError

    creds = NotionCredentials(integration_token="secret_test")
    pub = NotionPublisher(creds, parent_page_id=PARENT_ID)

    with (
        patch.dict(sys.modules, {"notion_client": None}),
        pytest.raises(_NotionImportError, match=r"iac-cartographer\[notion\]"),
    ):
        await pub.__aenter__()


# ── close releases the client ────────────────────────────────────────


async def test_aexit_closes_the_async_client() -> None:
    client = _mk_client()
    pub = await _publisher_with_client(client)
    await pub.__aexit__(None, None, None)
    client.aclose.assert_awaited_once()
    assert pub._client is None


# ── Pydantic / wiring sanity ─────────────────────────────────────────


def test_notion_credentials_requires_integration_token() -> None:
    """Empty payload is rejected — same fail-loud contract as the
    other credential models."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NotionCredentials.model_validate({})


# ── replace_blocks pagination (archiving > 100 existing blocks) ──────


async def test_replace_blocks_paginates_archive_when_page_has_more_than_100_blocks() -> None:
    """When the page being updated has more blocks than fit in one
    Notion list response (cap: 100/page), the publisher must follow
    `next_cursor` across multiple list calls and collect every block
    ID before deleting. Without the cursor follow, the second batch
    of old blocks would survive the update and the page would mix
    old + new content.
    """
    client = _mk_client()
    page_block_ids_1 = [f"old-block-{i}" for i in range(100)]
    page_block_ids_2 = [f"old-block-{i}" for i in range(100, 150)]
    client.blocks.children.list.side_effect = [
        # 1. parent's children.list → finds existing child page
        {
            "results": [{"id": CHILD_ID, "type": "child_page", "child_page": {"title": "acme/main"}}],
            "has_more": False,
        },
        # 2. read_sha → old sha
        {
            "results": [
                {"type": "callout", "callout": {"rich_text": [{"text": {"content": "iac-cartographer SHA: old"}}]}}
            ],
        },
        # 3. _replace_blocks archive list, page 1 (100 blocks, has_more=True)
        {
            "results": [{"id": bid} for bid in page_block_ids_1],
            "has_more": True,
            "next_cursor": "archive-cursor-1",
        },
        # 4. _replace_blocks archive list, page 2 (50 more blocks, has_more=False)
        {
            "results": [{"id": bid} for bid in page_block_ids_2],
            "has_more": False,
        },
    ]

    pub = await _publisher_with_client(client)
    result = await pub.publish_child(
        _inv(),
        sha="new",
        updated_at=datetime(2026, 5, 26, tzinfo=UTC),
        pipeline_url=None,
    )

    assert result.action == "updated"
    # The publisher must have followed the cursor on the second archive list call.
    archive_calls = client.blocks.children.list.await_args_list[2:]
    assert archive_calls[0].kwargs.get("start_cursor") is None
    assert archive_calls[1].kwargs["start_cursor"] == "archive-cursor-1"
    # All 150 old blocks were deleted (not just the first 100).
    assert client.blocks.delete.await_count == 150
    deleted_ids = {call.kwargs["block_id"] for call in client.blocks.delete.await_args_list}
    assert deleted_ids == set(page_block_ids_1 + page_block_ids_2)


# ── publish_overview existing-page paths ──────────────────────────────


async def test_publish_overview_skips_write_when_sha_unchanged() -> None:
    """Banner-SHA short-circuit on the overview page itself, mirroring
    the per-child page short-circuit. Same idempotency contract.

    NB: the SHA regex in notion_renderer is `[0-9a-f]+`, so the
    mocked banner content + the `sha=` arg must be valid hex —
    "sha-overview" wouldn't match and would silently fall through
    to the update path."""
    client = _mk_client()
    matching_sha = "deadbeef1234"
    client.blocks.children.list.side_effect = [
        # parent.children.list → finds existing "Overview" sub-page
        {
            "results": [{"id": "overview-id", "type": "child_page", "child_page": {"title": "Overview"}}],
            "has_more": False,
        },
        # read_sha on the overview page → matching sha (hex)
        {
            "results": [
                {
                    "type": "callout",
                    "callout": {"rich_text": [{"text": {"content": f"iac-cartographer SHA: {matching_sha}"}}]},
                }
            ],
        },
    ]

    pub = await _publisher_with_client(client)
    inv = _inv()
    result = await pub.publish_overview(
        [inv],
        {inv.meta.full_name: CHILD_ID},
        sha=matching_sha,
        updated_at=datetime(2026, 5, 26, tzinfo=UTC),
        pipeline_url=None,
    )

    assert result.action == "unchanged"
    assert result.page_id == "overview-id"
    # Skipped both the archive list and any append.
    client.pages.create.assert_not_awaited()
    client.blocks.children.append.assert_not_awaited()
    client.blocks.delete.assert_not_awaited()


async def test_publish_overview_replaces_blocks_when_sha_differs() -> None:
    """When the overview page exists with a different SHA, archive +
    re-append the new content (same path as the per-child update)."""
    client = _mk_client()
    client.blocks.children.list.side_effect = [
        # parent.children.list → finds existing overview
        {
            "results": [{"id": "overview-id", "type": "child_page", "child_page": {"title": "Overview"}}],
            "has_more": False,
        },
        # read_sha → old sha
        {
            "results": [
                {"type": "callout", "callout": {"rich_text": [{"text": {"content": "iac-cartographer SHA: old"}}]}}
            ],
        },
        # _replace_blocks archive list
        {"results": [{"id": "old-block-1"}, {"id": "old-block-2"}], "has_more": False},
    ]

    pub = await _publisher_with_client(client)
    inv = _inv()
    result = await pub.publish_overview(
        [inv],
        {inv.meta.full_name: CHILD_ID},
        sha="sha-overview-new",
        updated_at=datetime(2026, 5, 26, tzinfo=UTC),
        pipeline_url=None,
    )

    assert result.action == "updated"
    assert result.page_id == "overview-id"
    assert client.blocks.delete.await_count == 2
    # New blocks appended; first block carries the new SHA.
    appended = client.blocks.children.append.await_args.kwargs["children"]
    assert "iac-cartographer SHA: sha-overview-new" in appended[0]["callout"]["rich_text"][0]["text"]["content"]


# ── _find_child_by_title protective bound ─────────────────────────────


async def test_find_child_caps_pagination_at_50_pages() -> None:
    """`_find_child_by_title` is bounded at 50 page-walk iterations as
    defence against a misbehaving Notion API returning a perpetual
    `has_more=True` (would otherwise infinite-loop). After 50 unmatched
    pages it returns None, which surfaces upstream as "no existing
    child" — the publisher then attempts to create rather than hang."""
    client = _mk_client()
    # Every list call returns one non-matching child + has_more=True with
    # a fresh cursor — simulates the pathological "never exhausts" case.
    client.blocks.children.list.return_value = {
        "results": [{"id": "wrong", "type": "child_page", "child_page": {"title": "not-the-match"}}],
        "has_more": True,
        "next_cursor": "perpetual-cursor",
    }
    client.pages.create.return_value = {"id": "new-page-id"}

    pub = await _publisher_with_client(client)
    result = await pub.publish_child(
        _inv(),
        sha="abc",
        updated_at=datetime(2026, 5, 26, tzinfo=UTC),
        pipeline_url=None,
    )

    # The bound kicked in: 50 list calls, then "no existing" → create path.
    assert client.blocks.children.list.await_count == 50
    assert result.action == "created"
    assert result.page_id == "new-page-id"


# Avoid type-checker complaint about unused SimpleNamespace import in
# older test scaffolding.
_ = SimpleNamespace
