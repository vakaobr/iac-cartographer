"""Gitea / Forgejo discovery — list repos in one or more orgs.

**One source covers both platforms.** Forgejo forked from Gitea in 2022
and intentionally preserves API compatibility — `GET /api/v1/orgs/{org}/repos`
+ `GET /api/v1/repos/{owner}/{repo}/branches/{branch}` work identically
on both. Operators running either platform point this source at their
instance's base URL.

Strategy (mirrors `BitbucketDiscovery`): Gitea's free-tier code-search
API is per-repo (`GET /api/v1/repos/{owner}/{repo}/search`), not
org-wide — and many self-hosted instances disable the code indexer
entirely. So we enumerate every repository in each configured org via
`GET /api/v1/orgs/{org}/repos` and let the downstream
fetcher + extractor filter out the ones with no `.tf` files (they
produce an empty `TerraformSummary` and render as such).

Operators with large mixed-workload Gitea orgs should narrow the scope
with `discovery.deny_repos` glob patterns, or switch to the file-based
source for a hand-curated list.

Auth: bearer token via `Authorization: token <token>` (Gitea's
canonical scheme — note `token` not `Bearer`). Generate one at
`<base_url>/-/user/settings/applications`. Token scopes needed:
`read:organization` + `read:repository` for the listing + branch reads.
"""

from __future__ import annotations

import asyncio
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
    from iac_cartographer.models import GiteaCredentials

logger = logging.getLogger("iac_cartographer.discovery.gitea")


class GiteaDiscovery(DiscoverySource):
    """Discover repositories under one or more Gitea / Forgejo orgs."""

    name = "gitea"

    def __init__(
        self,
        creds: GiteaCredentials,
        orgs: list[str],
        base_url: str,
    ) -> None:
        # Gitea's auth scheme is `Authorization: token <token>` — NOT
        # `Bearer <token>` like GitHub. Bearer auth returns 401 on
        # Gitea instances; this is the most common operator-side
        # confusion when porting a config from GitHub.
        self._headers = {
            "Authorization": f"token {creds.token}",
            "Accept": "application/json",
        }
        # Strip trailing slash so the per-call paths stay clean
        # (`/api/v1/orgs/...`, not `//api/v1/orgs/...`).
        self._base_url = base_url.rstrip("/")
        self._orgs = orgs

    async def discover(self) -> list[RepoMetadata]:
        if not self._orgs:
            return []
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT_S,
        ) as client:
            tasks = [self._discover_org(client, org) for org in self._orgs]
            per_org = await asyncio.gather(*tasks)

        # Dedup across orgs in case the operator listed the same org
        # twice (or two orgs that contain the same fork). First-seen
        # wins — orchestrator-level dedup catches cross-source
        # duplicates too, but staying clean here keeps the per-source
        # log lines accurate.
        merged: dict[str, RepoMetadata] = {}
        for repos in per_org:
            for r in repos:
                merged.setdefault(r.full_name, r)
        return list(merged.values())

    async def _discover_org(self, client: httpx.AsyncClient, org: str) -> list[RepoMetadata]:
        repos = await self._list_org_repos(client, org)
        logger.info("gitea: org %s → %d repos enumerated (extractor filters .tf-bearing ones)", org, len(repos))
        # Fetch last-commit metadata in parallel — same shape as the
        # other sources. Per-repo failures are isolated (warn + drop)
        # so one bad repo doesn't sink the org.
        tasks = [self._fetch_last_commit(client, repo) for repo in repos]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[RepoMetadata] = []
        for r in results:
            if isinstance(r, RepoMetadata):
                out.append(r)
            elif isinstance(r, Exception):
                logger.warning("gitea: skipped repo (metadata fetch failed): %s", r)
        return out

    async def _list_org_repos(self, client: httpx.AsyncClient, org: str) -> list[dict]:
        """List every repo under `org`. Gitea paginates at `?page=N&limit=K`
        — same shape as GitHub. Hard-cap at `MAX_PAGES` (defence
        against an API quirk looping forever)."""
        repos: list[dict] = []
        page = 1
        while page <= MAX_PAGES:
            resp = await client.get(
                f"/api/v1/orgs/{org}/repos",
                params={"page": page, "limit": 50},
            )
            if resp.status_code == 404:
                # Org doesn't exist — log + treat as empty rather than
                # blow up the whole run. Operators sometimes deconfigure
                # an org without updating iac-cartographer's config.
                logger.warning("gitea: org %s not found (404); treating as empty", org)
                return []
            if resp.status_code >= 400:
                raise DiscoveryError(f"gitea org list failed (org={org}, status={resp.status_code}): {resp.text[:200]}")
            batch = resp.json()
            if not isinstance(batch, list):
                # Gitea returns a plain list on success; an unexpected
                # shape (object with an `errors` key, etc.) is a config
                # bug worth surfacing loudly.
                raise DiscoveryError(f"gitea org list returned non-list payload for {org}: {type(batch).__name__}")
            if not batch:
                return repos
            repos.extend(batch)
            # Gitea's `Link` header carries `rel="next"` when there's
            # more — match the GitHub pattern. Some older Gitea
            # versions don't send the header at all; fall back to
            # comparing batch-size to the page limit.
            link = resp.headers.get("Link", "")
            if 'rel="next"' in link or len(batch) >= 50:
                page += 1
                continue
            break
        return repos

    async def _fetch_last_commit(self, client: httpx.AsyncClient, repo: dict) -> RepoMetadata:
        full_name: str = repo["full_name"]
        default_branch: str = repo.get("default_branch") or "main"
        clone_url: str = repo.get("clone_url") or repo.get("ssh_url") or ""
        web_url: str = repo.get("html_url") or f"{self._base_url}/{full_name}"

        resp = await client.get(f"/api/v1/repos/{full_name}/branches/{default_branch}")
        resp.raise_for_status()
        branch = resp.json()
        commit = branch["commit"]
        last_sha: str = commit["id"]
        last_at = _parse_iso8601(commit["timestamp"])
        # Gitea's branch payload carries author under commit.author;
        # the schema is `{name, email, username, ...}`. Forgejo
        # matches.
        author = commit.get("author") or {}
        last_author: str | None = author.get("name") or author.get("username") or None

        return RepoMetadata(
            host="gitea",
            full_name=full_name,
            clone_url=clone_url,
            web_url=web_url,
            default_branch=default_branch,
            last_commit_sha=last_sha,
            last_commit_at=last_at,
            last_commit_author=last_author,
        )
