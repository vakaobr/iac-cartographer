"""Bitbucket Cloud discovery — list repos in one or more workspaces.

Bitbucket Cloud's public API has no `extension:tf`-style code search
endpoint comparable to GitHub or GitLab (workspace-level search exists
only on Premium plans and is awkward to authenticate). So this source
takes a different shape from `GitlabDiscovery` / `GithubDiscovery`:

  * Enumerate every repository under each configured workspace via
    `GET /2.0/repositories/{workspace}?role=member`.
  * Return metadata for all of them. The downstream fetcher + extractor
    already handle repos with zero `.tf` files gracefully (they produce
    an empty `TerraformSummary` and the page renders as such), so we
    don't lose correctness — just efficiency.

Operators with large mixed-workload workspaces should narrow the scope
with `discovery.deny_repos` glob patterns, or switch to the file-based
source for a hand-curated list.

Auth: Bitbucket Cloud accepts either:
  * a **workspace access token** via `Authorization: Bearer {token}`
    (recommended — scoped to a single workspace, easy to rotate), OR
  * a **username + app password** via HTTP Basic auth (legacy form,
    still widely used in scripts).

`BitbucketCredentials` accepts either; the chosen auth header is
constructed inside this source so the credential model stays narrow.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING

import httpx

from iac_cartographer.constants import DiscoveryError
from iac_cartographer.discovery.base import (
    DEFAULT_TIMEOUT_S,
    MAX_PAGES,
    DiscoverySource,
    _parse_iso8601,
)
from iac_cartographer.models import RepoMetadata

if TYPE_CHECKING:
    from iac_cartographer.models import BitbucketCredentials

logger = logging.getLogger("iac_cartographer.discovery.bitbucket")

BITBUCKET_BASE_URL = "https://api.bitbucket.org"


class BitbucketDiscovery(DiscoverySource):
    """Discover repositories under one or more Bitbucket Cloud workspaces."""

    name = "bitbucket"

    def __init__(
        self,
        creds: BitbucketCredentials,
        workspaces: list[str],
        base_url: str = BITBUCKET_BASE_URL,
    ) -> None:
        self._headers = _build_auth_headers(creds)
        self._base_url = base_url
        self._workspaces = workspaces

    async def discover(self) -> list[RepoMetadata]:
        if not self._workspaces:
            return []
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT_S,
        ) as client:
            tasks = [self._discover_workspace(client, ws) for ws in self._workspaces]
            per_workspace = await asyncio.gather(*tasks)

        merged: dict[str, RepoMetadata] = {}
        for repos in per_workspace:
            for r in repos:
                merged[r.full_name] = r
        return list(merged.values())

    async def _discover_workspace(self, client: httpx.AsyncClient, workspace: str) -> list[RepoMetadata]:
        # Stage 1 — enumerate every repo in the workspace.
        repo_stubs = await self._list_workspace_repos(client, workspace)

        # Stage 2 — fan out per-repo branch lookups so each repo gets its
        # real HEAD SHA. The list-repos response only carries `updated_on`
        # which is a coarse stand-in for "last activity"; banner-SHA
        # idempotency (and the rendered "last commit" hash) needs the
        # actual commit ID.
        tasks = [self._fetch_repo_metadata(client, stub) for stub in repo_stubs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[RepoMetadata] = []
        for r in results:
            if isinstance(r, RepoMetadata):
                out.append(r)
            elif isinstance(r, Exception):
                logger.warning("bitbucket: skipped repo (metadata fetch failed): %s", r)

        logger.info("bitbucket: workspace %s → %d repos", workspace, len(out))
        return out

    async def _list_workspace_repos(self, client: httpx.AsyncClient, workspace: str) -> list[dict]:
        url: str | None = f"/2.0/repositories/{workspace}"
        params: dict[str, str] | None = {"role": "member", "pagelen": "100"}
        stubs: list[dict] = []
        page_count = 0
        # Bitbucket's pagination is a `next` URL on the payload itself —
        # follow it until exhausted, capped by MAX_PAGES for safety.
        while url is not None and page_count < MAX_PAGES:
            resp = await client.get(url, params=params)
            params = None  # only set on first call; `next` URL has params baked in
            if resp.status_code == 404:
                raise DiscoveryError(f"bitbucket workspace {workspace!r} not found (or token lacks access)")
            if resp.status_code >= 400:
                raise DiscoveryError(
                    f"bitbucket list repos failed (workspace={workspace}, status={resp.status_code}): {resp.text[:200]}"
                )
            data = resp.json()
            stubs.extend(data.get("values", []))
            next_url = data.get("next")
            if not isinstance(next_url, str):
                break
            url = next_url.removeprefix(self._base_url)
            page_count += 1
        return stubs

    async def _fetch_repo_metadata(self, client: httpx.AsyncClient, stub: dict) -> RepoMetadata | None:
        """Enrich a list-repos stub with the default branch's HEAD commit.

        Returns `None` for repos that can't be fully resolved (no main
        branch, missing clone URL, etc.) — the caller filters those out."""
        full_name = stub.get("full_name")
        if not isinstance(full_name, str):
            return None

        mainbranch = (stub.get("mainbranch") or {}).get("name")
        if not isinstance(mainbranch, str):
            # Empty repos sometimes report null mainbranch. Skip — there's
            # nothing to clone or analyse anyway.
            return None

        # Prefer the HTTPS clone URL so the same auth path as the API works.
        clone_links = (stub.get("links") or {}).get("clone") or []
        clone_url = next(
            (link.get("href") for link in clone_links if link.get("name") == "https"),
            None,
        )
        if not isinstance(clone_url, str):
            return None

        web_url = ((stub.get("links") or {}).get("html") or {}).get("href")
        if not isinstance(web_url, str):
            web_url = f"https://bitbucket.org/{full_name}"

        # Branch endpoint carries the HEAD commit + its date. One extra
        # round-trip per repo, batched across repos via asyncio.gather.
        branch_resp = await client.get(f"/2.0/repositories/{full_name}/refs/branches/{mainbranch}")
        branch_resp.raise_for_status()
        branch = branch_resp.json()
        target = branch.get("target") or {}
        last_sha = target.get("hash")
        last_at_str = target.get("date")
        if not isinstance(last_sha, str) or not isinstance(last_at_str, str):
            return None
        author_block = (target.get("author") or {}).get("raw")
        last_author = author_block if isinstance(author_block, str) else None

        return RepoMetadata(
            host="bitbucket",
            full_name=full_name,
            clone_url=clone_url,
            web_url=web_url,
            default_branch=mainbranch,
            last_commit_sha=last_sha,
            last_commit_at=_parse_iso8601(last_at_str),
            last_commit_author=last_author,
        )


def _build_auth_headers(creds: BitbucketCredentials) -> dict[str, str]:
    """Bearer if access_token; HTTP Basic if username+app_password.

    `BitbucketCredentials.model_validate` enforces the XOR so we can rely
    on exactly one of the two being populated here."""
    if creds.access_token is not None:
        return {
            "Authorization": f"Bearer {creds.access_token}",
            "Accept": "application/json",
        }
    # username + app_password path.
    raw = f"{creds.username}:{creds.app_password}".encode()
    encoded = base64.b64encode(raw).decode("ascii")
    return {
        "Authorization": f"Basic {encoded}",
        "Accept": "application/json",
    }
