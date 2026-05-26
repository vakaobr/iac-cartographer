"""Notion publisher — upsert each repo as a sub-page of a parent.

Shape:

  * Operator pre-creates a Notion page and **shares it with the
    iac-cartographer integration** (via Notion's Connections menu).
    `parent_page_id` in the config points at this page.
  * Each repo becomes a sub-page of the parent, titled `full_name`
    (e.g. `acme-org/main-cluster`).
  * The overview is a separate sub-page titled "Overview" carrying the
    aggregate summary + cross-links to every repo's deep-dive page.
  * Idempotency lives in a `callout` block at the top of every page
    (`🔖 iac-cartographer SHA: <hex>`). The next run reads the first
    block, parses the SHA, and short-circuits when it matches —
    same contract as the other publishers, just embedded in a
    Notion-native carrier.

Requires `pip install 'iac-cartographer[notion]'` — `notion-client`
is the official SDK and is lazy-imported on first publish so the
base install doesn't pay for it.

Auth: integration token from the `iac-cartographer/notion` secret.
Create the integration at notion.so/profile/integrations → "Internal";
share the parent page with it via the page's Connections menu.

Block-replacement caveat: Notion's API has no "replace page body"
operation. Updates go through:

  1. List the page's existing block children.
  2. Delete each one (archive=True).
  3. Append the new blocks.

This means each update sends ~2N HTTP calls (N deletes + N inserts).
For a typical iac-cartographer page (~15 blocks) that's ~30 calls per
update — not free, but acceptable at the once-per-week cadence the
runtime is designed for. The banner-SHA short-circuit means unchanged
pages skip the rewrite entirely.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from iac_cartographer.publishers.base import Publisher, PublishResult
from iac_cartographer.publishers.notion_renderer import (
    extract_banner_sha,
    render_child_blocks,
    render_overview_blocks,
)

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import NotionCredentials, RepoInventory

logger = logging.getLogger("iac_cartographer.publishers.notion")

_OVERVIEW_TITLE = "Overview"
# Notion's PATCH children endpoint accepts up to 100 blocks per call.
# We chunk below the limit.
_APPEND_CHUNK = 90


class _NotionImportError(ImportError):
    """Raised when notion-client isn't installed at first publish."""


class NotionPublisher(Publisher):
    """Publishes the inventory as Notion sub-pages."""

    def __init__(
        self,
        creds: NotionCredentials,
        *,
        parent_page_id: str,
    ) -> None:
        self._token = creds.integration_token
        self._parent_page_id = parent_page_id
        self._client: Any | None = None  # AsyncClient — lazy + optional dep

    async def __aenter__(self) -> NotionPublisher:
        self._client = self._make_client()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _make_client(self) -> Any:
        """Lazy-import notion-client. Raises with a clear pip-install hint
        when the optional dep is missing — same defence the email
        channel uses for aiosmtplib."""
        try:
            from notion_client import AsyncClient
        except ImportError as exc:
            raise _NotionImportError("Notion publisher requires `pip install 'iac-cartographer[notion]'`") from exc
        return AsyncClient(auth=self._token)

    # ─── child upsert ─────────────────────────────────────────────────

    async def publish_child(
        self,
        inv: RepoInventory,
        *,
        sha: str,
        updated_at: datetime,
        pipeline_url: str | None,
    ) -> PublishResult:
        assert self._client is not None, "NotionPublisher must be used as an async context manager"

        title = inv.meta.full_name
        existing_id = await self._find_child_by_title(title)
        blocks = render_child_blocks(inv, sha=sha, updated_at=updated_at, pipeline_url=pipeline_url)

        if existing_id:
            prior_sha = await self._read_sha(existing_id)
            if prior_sha == sha:
                logger.info("notion: %s — unchanged (sha=%s); skipping write", title, sha)
                return PublishResult(page_id=existing_id, action="unchanged")
            await self._replace_blocks(existing_id, blocks)
            logger.info("notion: %s — updated (sha=%s)", title, sha)
            return PublishResult(page_id=existing_id, action="updated")

        new_id = await self._create_sub_page(title=title, blocks=blocks)
        logger.info("notion: %s — created (sha=%s)", title, sha)
        return PublishResult(page_id=new_id, action="created")

    # ─── overview ─────────────────────────────────────────────────────

    async def publish_overview(
        self,
        inventories: list[RepoInventory],
        child_page_ids: dict[str, str],
        *,
        sha: str,
        updated_at: datetime,
        pipeline_url: str | None,
    ) -> PublishResult:
        assert self._client is not None, "NotionPublisher must be used as an async context manager"

        existing_id = await self._find_child_by_title(_OVERVIEW_TITLE)
        blocks = render_overview_blocks(
            inventories,
            child_page_ids,
            sha=sha,
            updated_at=updated_at,
            pipeline_url=pipeline_url,
        )

        if existing_id:
            prior_sha = await self._read_sha(existing_id)
            if prior_sha == sha:
                logger.info("notion: overview — unchanged (sha=%s); skipping write", sha)
                return PublishResult(page_id=existing_id, action="unchanged")
            await self._replace_blocks(existing_id, blocks)
            logger.info("notion: overview — updated (sha=%s)", sha)
            return PublishResult(page_id=existing_id, action="updated")

        new_id = await self._create_sub_page(title=_OVERVIEW_TITLE, blocks=blocks)
        logger.info("notion: overview — created (sha=%s)", sha)
        return PublishResult(page_id=new_id, action="created")

    # ─── internals ────────────────────────────────────────────────────

    async def _find_child_by_title(self, title: str) -> str | None:
        """Walk the parent's child blocks looking for a sub-page with the
        given title. Returns the page UUID on match, None otherwise."""
        cursor: str | None = None
        for _ in range(50):  # cap pagination, defence-in-depth
            kwargs: dict[str, Any] = {"block_id": self._parent_page_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = await self._client.blocks.children.list(**kwargs)
            for block in resp.get("results", []):
                if block.get("type") == "child_page" and block.get("child_page", {}).get("title") == title:
                    return block["id"]
            if not resp.get("has_more"):
                return None
            cursor = resp.get("next_cursor")
        return None

    async def _read_sha(self, page_id: str) -> str | None:
        """Fetch the page's first block and parse the banner-SHA out of
        it. Returns None if the first block isn't our SHA callout, or
        if the page has no blocks at all (someone hand-cleared it)."""
        resp = await self._client.blocks.children.list(block_id=page_id, page_size=1)
        results = resp.get("results", [])
        return extract_banner_sha(results[0] if results else None)

    async def _create_sub_page(self, *, title: str, blocks: list[dict[str, Any]]) -> str:
        """Create a new sub-page under the configured parent. Returns the
        new page's UUID."""
        # The Notion API caps `children` on page create at 100 blocks.
        # Our pages are well below that, but chunk anyway against
        # future-proofing.
        initial = blocks[:_APPEND_CHUNK]
        rest = blocks[_APPEND_CHUNK:]

        page = await self._client.pages.create(
            parent={"page_id": self._parent_page_id},
            properties={
                "title": {
                    "title": [{"type": "text", "text": {"content": title}}],
                }
            },
            children=initial,
        )
        page_id: str = page["id"]
        if rest:
            await self._append_chunked(page_id, rest)
        return page_id

    async def _replace_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        """Delete all existing children of the page, then append the new
        block list. Notion has no "replace body" op — this is the
        idiomatic path used by every Notion sync tool."""
        # 1. Archive existing children one-by-one. Notion treats
        #    `blocks.update(archived=True)` as soft-delete; the page
        #    keeps the deleted block in its trash for ~30 days.
        cursor: str | None = None
        existing_ids: list[str] = []
        for _ in range(50):
            kwargs: dict[str, Any] = {"block_id": page_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = await self._client.blocks.children.list(**kwargs)
            existing_ids.extend(b["id"] for b in resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        for block_id in existing_ids:
            await self._client.blocks.delete(block_id=block_id)

        # 2. Append the new content in 90-block chunks.
        await self._append_chunked(page_id, blocks)

    async def _append_chunked(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        """PATCH children in <=90-block chunks to stay below Notion's
        per-call limit."""
        for start in range(0, len(blocks), _APPEND_CHUNK):
            chunk = blocks[start : start + _APPEND_CHUNK]
            await self._client.blocks.children.append(block_id=page_id, children=chunk)
