"""Confluence Cloud v2 API client — find-or-create + version-aware PUT.

Scoped to the operations iac-cartographer needs:

  * `get_space_id_by_key` — resolve the DevOps space ID once at startup.
  * `find_child_by_title`  — locate an existing child page under a parent.
  * `get_page`             — fetch the current ADF body + version number.
  * `upsert`               — idempotent create-or-update with banner-SHA short-circuit.

Auth is Atlassian HTTP Basic — `email:api_token` base64-encoded. This
expects a **legacy (unscoped) API token** created at id.atlassian.com via
the plain "Create API token" form (no app / scopes selection). Atlassian's
newer "Create API token with scopes" tokens are OAuth-app-bound and reject
HTTP Basic — they need an installed Forge/Connect app on the workspace
that grants the token access. For most self-service iac-cartographer
deployments, the legacy token is the path of least resistance; for org-wide
deployments with a service-account, use the scoped variant once an admin
has installed the right app. Body representation is always
`atlas_doc_format`.

One 409 retry is supported (race against a manual editor); after that we
raise `ConfluenceError` to the orchestrator, which records the per-page
failure but doesn't abort the run.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx

from iac_cartographer.constants import ConfluenceError
from iac_cartographer.renderer import extract_banner_sha

if TYPE_CHECKING:
    from iac_cartographer.models import ConfluenceCredentials

logger = logging.getLogger("iac_cartographer.confluence")

DEFAULT_TIMEOUT_S = 30.0
MAX_PAGES = 50  # safety cap on cursor pagination — guards against API quirks looping
UpsertAction = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class UpsertResult:
    page_id: str
    action: UpsertAction
    version: int


@dataclass(frozen=True)
class _PageView:
    id: str
    title: str
    version: int
    body_adf: dict[str, Any] | None


class ConfluenceClient:
    def __init__(self, site: str, creds: ConfluenceCredentials) -> None:
        # HTTP Basic for legacy (unscoped) Atlassian API tokens. See module
        # docstring for why scoped tokens aren't workable today.
        auth_raw = f"{creds.email}:{creds.api_token}".encode()
        auth_b64 = base64.b64encode(auth_raw).decode("ascii")
        self._headers = {
            "Authorization": f"Basic {auth_b64}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._base_url = f"https://{site}/wiki/api/v2"
        self._space_id_cache: dict[str, str] = {}

    # ─── Spaces ─────────────────────────────────────────────────────────

    async def get_space_id_by_key(self, client: httpx.AsyncClient, key: str) -> str:
        if key in self._space_id_cache:
            return self._space_id_cache[key]
        resp = await client.get("/spaces", params={"keys": key, "limit": 1})
        if resp.status_code == 401:
            raise ConfluenceError("confluence auth failed (401) — rotate token")
        if resp.status_code >= 400:
            raise ConfluenceError(
                f"confluence space lookup failed (key={key}, status={resp.status_code}): {resp.text[:200]}"
            )
        results = resp.json().get("results") or []
        if not results:
            raise ConfluenceError(f"confluence space not found: {key}")
        space_id: str = str(results[0]["id"])
        self._space_id_cache[key] = space_id
        return space_id

    # ─── Pages ──────────────────────────────────────────────────────────

    async def find_child_by_title(self, client: httpx.AsyncClient, parent_id: str, title: str) -> _PageView | None:
        """Walk the parent's children, paginated; return the first match by title.

        Bounded by `MAX_PAGES` and a "cursor did not advance" guard so a
        misbehaving upstream cannot infinite-loop us.
        """
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page_num in range(MAX_PAGES):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(f"/pages/{parent_id}/children", params=params)
            if resp.status_code == 401:
                raise ConfluenceError("confluence auth failed (401) — rotate token")
            if resp.status_code >= 400:
                raise ConfluenceError(
                    f"confluence list-children failed (parent={parent_id}, status={resp.status_code}): "
                    f"{resp.text[:200]}"
                )
            payload = resp.json()
            for child in payload.get("results", []) or []:
                if child.get("title") == title:
                    # We have title + id from this call; fetch the body via get_page
                    # so the upsert path has the version + banner SHA.
                    return await self.get_page(client, str(child["id"]))
            next_cursor = _extract_next_cursor(payload, resp.headers)
            if not next_cursor:
                return None
            if next_cursor in seen_cursors:
                logger.warning(
                    "confluence: pagination cursor repeated (%s); treating as exhausted",
                    next_cursor,
                )
                return None
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        logger.warning("confluence: hit MAX_PAGES=%d on find_child_by_title; giving up", MAX_PAGES)
        return None

    async def get_page(self, client: httpx.AsyncClient, page_id: str) -> _PageView:
        resp = await client.get(
            f"/pages/{page_id}",
            params={"body-format": "atlas_doc_format"},
        )
        if resp.status_code == 401:
            raise ConfluenceError("confluence auth failed (401) — rotate token")
        if resp.status_code == 404:
            raise ConfluenceError(f"confluence page not found: {page_id}")
        if resp.status_code >= 400:
            raise ConfluenceError(
                f"confluence get-page failed ({page_id}, status={resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        adf_str = (data.get("body") or {}).get("atlas_doc_format", {}).get("value")
        body_adf: dict[str, Any] | None = None
        if isinstance(adf_str, str) and adf_str:
            try:
                body_adf = json.loads(adf_str)
            except json.JSONDecodeError:
                logger.warning("confluence: page %s body is non-JSON ADF — treating as unparsable", page_id)
                body_adf = None
        return _PageView(
            id=str(data["id"]),
            title=str(data["title"]),
            version=int(data.get("version", {}).get("number", 1)),
            body_adf=body_adf,
        )

    # ─── upsert (the only mutator) ─────────────────────────────────────

    async def upsert(
        self,
        client: httpx.AsyncClient,
        space_id: str,
        parent_id: str,
        title: str,
        adf_body: dict[str, Any],
        current_sha: str,
    ) -> UpsertResult:
        """Create or update a page; skip the PUT if the banner SHA matches.

        `current_sha` is the SHA the *new* body's banner advertises (i.e. the
        SHA we computed from the latest `RepoInventory`). We compare it to the
        existing page's banner; if equal, no-op.
        """
        existing = await self.find_child_by_title(client, parent_id, title)
        if existing is None:
            return await self._create(client, space_id, parent_id, title, adf_body)

        prior_sha = extract_banner_sha(existing.body_adf)
        if prior_sha is not None and prior_sha == current_sha:
            logger.info("confluence: %s — unchanged (sha=%s); skipping PUT", title, current_sha)
            return UpsertResult(page_id=existing.id, action="unchanged", version=existing.version)

        return await self._update_with_one_retry(client, existing, title, adf_body)

    async def upsert_existing_page(
        self,
        client: httpx.AsyncClient,
        page_id: str,
        title: str,
        adf_body: dict[str, Any],
        current_sha: str,
    ) -> UpsertResult:
        """Update an existing page identified by `page_id` in place.

        Use when we own the page identity directly (e.g. the manually
        pre-created parent stub doubles as our overview page — the parent's
        title equals our OVERVIEW_TITLE, so `upsert` cannot create a child
        with the same title under it). Banner-SHA short-circuit still applies.
        """
        existing = await self.get_page(client, page_id)
        prior_sha = extract_banner_sha(existing.body_adf)
        if prior_sha is not None and prior_sha == current_sha:
            logger.info(
                "confluence: %s (page_id=%s) — unchanged (sha=%s); skipping PUT",
                title,
                page_id,
                current_sha,
            )
            return UpsertResult(page_id=existing.id, action="unchanged", version=existing.version)
        return await self._update_with_one_retry(client, existing, title, adf_body)

    async def _create(
        self,
        client: httpx.AsyncClient,
        space_id: str,
        parent_id: str,
        title: str,
        adf_body: dict[str, Any],
    ) -> UpsertResult:
        payload = {
            "spaceId": space_id,
            "parentId": parent_id,
            "status": "current",
            "title": title,
            "body": {"representation": "atlas_doc_format", "value": json.dumps(adf_body)},
        }
        resp = await client.post("/pages", json=payload)
        if resp.status_code == 401:
            raise ConfluenceError("confluence auth failed (401) — rotate token")
        if resp.status_code >= 400:
            raise ConfluenceError(
                f"confluence create-page failed ({title}, status={resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        return UpsertResult(
            page_id=str(data["id"]),
            action="created",
            version=int(data.get("version", {}).get("number", 1)),
        )

    async def _update_with_one_retry(
        self,
        client: httpx.AsyncClient,
        existing: _PageView,
        title: str,
        adf_body: dict[str, Any],
    ) -> UpsertResult:
        result = await self._update_once(client, existing, title, adf_body)
        if result is not None:
            return result

        # One retry — re-GET the page (someone else bumped the version) and try once more.
        logger.warning("confluence: %s — 409 version conflict; refetching and retrying once", title)
        refetched = await self.get_page(client, existing.id)
        result = await self._update_once(client, refetched, title, adf_body)
        if result is not None:
            return result
        raise ConfluenceError(f"confluence: persistent 409 conflict on update for {title} (page_id={existing.id})")

    async def _update_once(
        self,
        client: httpx.AsyncClient,
        existing: _PageView,
        title: str,
        adf_body: dict[str, Any],
    ) -> UpsertResult | None:
        """Return None on 409 (caller will retry), an UpsertResult on success.
        Any other failure raises."""
        new_version = existing.version + 1
        payload = {
            "id": existing.id,
            "status": "current",
            "title": title,
            "version": {"number": new_version},
            "body": {"representation": "atlas_doc_format", "value": json.dumps(adf_body)},
        }
        resp = await client.put(f"/pages/{existing.id}", json=payload)
        if resp.status_code == 409:
            return None
        if resp.status_code == 401:
            raise ConfluenceError("confluence auth failed (401) — rotate token")
        if resp.status_code >= 400:
            raise ConfluenceError(
                f"confluence update-page failed ({title}, status={resp.status_code}): {resp.text[:200]}"
            )
        return UpsertResult(page_id=existing.id, action="updated", version=new_version)

    # ─── Session helper ──────────────────────────────────────────────

    def session(self) -> httpx.AsyncClient:
        """Return a configured `httpx.AsyncClient` for use as `async with client:`."""
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT_S,
        )


def _extract_next_cursor(payload: dict[str, Any], headers: httpx.Headers) -> str | None:
    """Confluence v2 paginates via `_links.next` (URL with a `cursor=` query),
    not via plain `cursor` field. We extract the cursor value from that URL.
    Some endpoints also emit a `Link` header — handled as a fallback."""
    links = payload.get("_links") or {}
    nxt = links.get("next")
    if isinstance(nxt, str) and "cursor=" in nxt:
        return nxt.split("cursor=", 1)[1].split("&", 1)[0]
    link_header = headers.get("Link", "")
    if 'rel="next"' in link_header and "cursor=" in link_header:
        tail = link_header.split("cursor=", 1)[1]
        # Stop at the first delimiter: `&`, `>`, `;`, or whitespace.
        for delim in ("&", ">", ";", " "):
            if delim in tail:
                tail = tail.split(delim, 1)[0]
        return tail or None
    return None
