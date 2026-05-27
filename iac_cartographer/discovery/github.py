"""GitHub discovery — `code search` for `.tf` files across one or more orgs.

Strategy: `GET /search/code?q=org:{org}+extension:tf` returns repos with at
least one `.tf` file. We then call `GET /repos/{o}/{r}` + branches API for
default-branch + last-commit metadata.
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
    from iac_cartographer.models import GithubCredentials

logger = logging.getLogger("iac_cartographer.discovery.github")

GITHUB_BASE_URL = "https://api.github.com"


class GithubDiscovery(DiscoverySource):
    """Discover Terraform-bearing repositories under one or more GitHub orgs."""

    name = "github"

    def __init__(
        self,
        creds: GithubCredentials,
        orgs: list[str] | None = None,
        base_url: str = GITHUB_BASE_URL,
    ) -> None:
        self._headers = {
            "Authorization": f"Bearer {creds.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # api.github.com (public + GitHub Enterprise Cloud) or a self-hosted
        # GHES REST base like `https://ghe.example.com/api/v3`. We do NOT
        # auto-append `/api/v3` — api.github.com doesn't use it, so the
        # operator includes it explicitly for GHES. Strip a trailing slash
        # so httpx's base-url join stays predictable.
        self._base_url = base_url.rstrip("/")
        self._orgs = orgs or []

    async def discover(self) -> list[RepoMetadata]:
        return await self.list_repos_with_terraform(self._orgs)

    async def list_repos_with_terraform(self, orgs: list[str]) -> list[RepoMetadata]:
        if not orgs:
            return []
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT_S,
        ) as client:
            tasks = [self._discover_org(client, org) for org in orgs]
            per_org = await asyncio.gather(*tasks)

        merged: dict[str, RepoMetadata] = {}
        for repos in per_org:
            for r in repos:
                merged[r.full_name] = r
        return list(merged.values())

    async def _discover_org(self, client: httpx.AsyncClient, org: str) -> list[RepoMetadata]:
        repo_full_names = await self._search_code_for_repo_names(client, org)
        logger.info("github: org %s → %d repos with .tf files", org, len(repo_full_names))
        tasks = [self._fetch_repo_metadata(client, name) for name in repo_full_names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[RepoMetadata] = []
        for r in results:
            if isinstance(r, RepoMetadata):
                out.append(r)
            elif isinstance(r, Exception):
                logger.warning("github: skipped repo (metadata fetch failed): %s", r)
        return out

    async def _search_code_for_repo_names(self, client: httpx.AsyncClient, org: str) -> list[str]:
        names: set[str] = set()
        page = 1
        while page <= MAX_PAGES:
            resp = await client.get(
                "/search/code",
                params={"q": f"org:{org} extension:tf", "per_page": 100, "page": page},
            )
            if resp.status_code == 422:
                # GitHub returns 422 if the query is invalid OR if no commits
                # match — for an org with no .tf files, treat as empty.
                logger.info("github: search/code returned 422 for org=%s — assuming empty", org)
                return sorted(names)
            if resp.status_code >= 400:
                raise DiscoveryError(
                    f"github code search failed (org={org}, status={resp.status_code}): {resp.text[:200]}"
                )
            data = resp.json()
            for item in data.get("items", []):
                repo = item.get("repository", {})
                full = repo.get("full_name")
                if isinstance(full, str):
                    names.add(full)
            link = resp.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            page += 1
        return sorted(names)

    async def _fetch_repo_metadata(self, client: httpx.AsyncClient, full_name: str) -> RepoMetadata:
        resp = await client.get(f"/repos/{full_name}")
        resp.raise_for_status()
        repo = resp.json()
        default_branch = repo.get("default_branch") or "main"

        # Last-commit lookup via the branches API matches GitLab's pattern.
        branch_resp = await client.get(f"/repos/{full_name}/branches/{default_branch}")
        branch_resp.raise_for_status()
        branch = branch_resp.json()
        commit = branch["commit"]
        last_sha = commit["sha"]
        last_at = _parse_iso8601(commit["commit"]["committer"]["date"])
        # GitHub nests author info under commit.commit.author; fall back to
        # the committer name (e.g. for squash-merged PRs the committer is
        # GitHub itself but the original author is preserved separately).
        commit_inner = commit.get("commit", {})
        last_author = (
            commit_inner.get("author", {}).get("name") or commit_inner.get("committer", {}).get("name") or None
        )

        return RepoMetadata(
            host="github",
            full_name=full_name,
            clone_url=repo["clone_url"],
            web_url=repo["html_url"],
            default_branch=default_branch,
            last_commit_sha=last_sha,
            last_commit_at=last_at,
            last_commit_author=last_author,
        )
