"""GitHub discovery — list org repos and probe each one's git tree for `.tf`.

Strategy: page `GET /orgs/{org}/repos?type=all`, then for each repo call
`GET /repos/{o}/{r}/git/trees/{default_branch}?recursive=1` and keep the
repos that have at least one `.tf` blob. We then call `GET /repos/{o}/{r}`
+ branches API for default-branch + last-commit metadata.

Why not `/search/code`? GitHub deprecated several `/search/code` query
fields on 2026-03-27. The practical effect for fine-grained PATs is that
`q=org:{org}+extension:tf` silently returns `total_count=0` for private
repos even when the same PAT can read those repos through the REST API.
The git-tree probe uses only the `Contents:read` permission the rest of
discovery already needs, so it works the moment the PAT is authorized
for the org.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

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
        repo_full_names = await self._list_repos_with_tf(client, org)
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

    async def _list_repos_with_tf(self, client: httpx.AsyncClient, org: str) -> list[str]:
        """List the org's repos and keep only those with `.tf` on the default branch.

        Uses `GET /orgs/{org}/repos` + a per-repo git-tree probe instead of
        `/search/code`, which became unreliable for fine-grained PATs after
        GitHub's 2026-03-27 API change.
        """
        names: list[str] = []
        page = 1
        while page <= MAX_PAGES:
            resp = await client.get(
                f"/orgs/{org}/repos",
                params={"type": "all", "per_page": 100, "page": page},
            )
            if resp.status_code >= 400:
                raise DiscoveryError(
                    f"github org listing failed (org={org}, status={resp.status_code}): {resp.text[:200]}"
                )
            repos = resp.json() or []
            candidates = [r for r in repos if r.get("default_branch") and r.get("full_name")]
            probes = await asyncio.gather(
                *(self._repo_has_tf(client, r) for r in candidates),
                return_exceptions=True,
            )
            for repo_obj, has_tf in zip(candidates, probes, strict=True):
                if isinstance(has_tf, Exception):
                    logger.warning("github: tree probe failed for %s: %s", repo_obj["full_name"], has_tf)
                    continue
                if has_tf:
                    names.append(repo_obj["full_name"])
            if 'rel="next"' not in resp.headers.get("Link", ""):
                break
            page += 1
        return sorted(set(names))

    async def _repo_has_tf(self, client: httpx.AsyncClient, repo: dict[str, Any]) -> bool:
        """True if the repo's default branch has any `.tf` blob in its git tree.

        Empty repos (no commits on the default branch) return 404 or 409 on
        the tree endpoint — these are silently skipped. If GitHub marks the
        tree response `truncated` (repos with >100k entries or >7MB JSON)
        and no `.tf` has surfaced, we log a warning and treat the repo as
        having none; per-directory tree walks aren't worth the request
        budget for typical IaC repositories.
        """
        full = repo["full_name"]
        branch = repo["default_branch"]
        resp = await client.get(f"/repos/{full}/git/trees/{branch}", params={"recursive": "1"})
        if resp.status_code in (404, 409):
            return False
        resp.raise_for_status()
        data = resp.json()
        tree = data.get("tree") or []
        has_tf = any(node.get("type") == "blob" and node.get("path", "").endswith(".tf") for node in tree)
        if data.get("truncated") and not has_tf:
            logger.warning(
                "github: %s tree truncated, no .tf seen in first page — repo may be too large to enumerate",
                full,
            )
        return has_tf

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
