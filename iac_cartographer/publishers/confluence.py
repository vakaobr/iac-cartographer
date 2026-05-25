"""Confluence Cloud publisher — wraps the v2 client in the Publisher contract.

Renders inventories to ADF (Atlassian Document Format) using the existing
`iac_cartographer.renderer` module, then upserts them via
`iac_cartographer.confluence.ConfluenceClient`. Page identity is the
numeric Confluence page ID; banner-SHA short-circuit comes from the
client's `upsert` + `upsert_existing_page` paths.

One quirk worth knowing: the overview page IS the parent page. Confluence
rejects creating a child whose title matches the parent's title, so we
update the parent in place rather than upserting a same-titled child
under it. See `publish_overview` below."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from iac_cartographer.publishers.base import Publisher, PublishResult
from iac_cartographer.renderer import build_child, build_overview

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.confluence import ConfluenceClient
    from iac_cartographer.models import ConfluenceConfig, RepoInventory


logger = logging.getLogger("iac_cartographer.publishers.confluence")


class ConfluencePublisher(Publisher):
    """Publishes the inventory to Atlassian Confluence Cloud.

    Constructor takes the low-level `ConfluenceClient` (which owns auth +
    HTTP), the `ConfluenceConfig` (space key, parent page lookup path),
    and the resolved parent page ID. The parent page ID is resolved
    upstream by the orchestrator's preflight check — passing it in here
    avoids a duplicate SSM read.

    On `__aenter__` we open the underlying httpx session and resolve the
    space ID. `__aexit__` closes the session."""

    def __init__(
        self,
        client: ConfluenceClient,
        config: ConfluenceConfig,
        parent_page_id: str,
    ) -> None:
        self._client = client
        self._config = config
        self._parent_page_id = parent_page_id
        self._session = None
        self._space_id: str | None = None

    async def __aenter__(self) -> ConfluencePublisher:
        # Open the client's httpx session and resolve the space ID once,
        # so per-page publish calls don't pay that cost N times.
        self._session_ctx = self._client.session()
        self._session = await self._session_ctx.__aenter__()
        self._space_id = await self._client.get_space_id_by_key(self._session, self._config.space_key)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._session_ctx is not None:
            await self._session_ctx.__aexit__(exc_type, exc, tb)
        self._session = None
        self._space_id = None

    async def publish_child(
        self,
        inv: RepoInventory,
        *,
        sha: str,
        updated_at: datetime,
        pipeline_url: str | None,
    ) -> PublishResult:
        assert self._session is not None and self._space_id is not None, (
            "ConfluencePublisher must be used as an `async with` context manager"
        )
        title, adf_body = build_child(inv, sha=sha, updated_at=updated_at, pipeline_url=pipeline_url)
        result = await self._client.upsert(
            self._session,
            space_id=self._space_id,
            parent_id=self._parent_page_id,
            title=title,
            adf_body=adf_body,
            current_sha=sha,
        )
        return PublishResult(page_id=result.page_id, action=result.action)

    async def publish_overview(
        self,
        inventories: list[RepoInventory],
        child_page_ids: dict[str, str],
        *,
        sha: str,
        updated_at: datetime,
        pipeline_url: str | None,
    ) -> PublishResult:
        assert self._session is not None, "ConfluencePublisher must be used as an `async with` context manager"
        _title, adf_body = build_overview(
            inventories,
            child_page_ids,
            sha=sha,
            updated_at=updated_at,
            space_key=self._config.space_key,
            pipeline_url=pipeline_url,
        )
        # The pre-created parent page IS the overview. Update in place;
        # Confluence forbids creating a child with the same title as its
        # parent (we'd hit a 400 "title already exists in parent").
        result = await self._client.upsert_existing_page(
            self._session,
            page_id=self._parent_page_id,
            title=_title,
            adf_body=adf_body,
            current_sha=sha,
        )
        return PublishResult(page_id=result.page_id, action=result.action)
