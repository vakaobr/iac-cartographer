"""GitLab discovery — `extension:tf` blob search across one or more groups.

Strategy: `GET /groups/{id}/search?scope=blobs&search=extension:tf` returns
blobs (file matches) across the group's repos including subgroups in modern
GitLab. We dedupe by `project_id` then enrich with a project-info + branch
call for the last-commit metadata.

Works against gitlab.com and self-hosted GitLab via the `base_url` arg.
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
    from iac_cartographer.models import GitlabCredentials

logger = logging.getLogger("iac_cartographer.discovery.gitlab")

# Default — the public hosted gitlab.com. For self-hosted GitLab instances,
# set `discovery.gitlab_base_url` in the YAML config (e.g.
# `https://gitlab.your-org.example`). The `/api/v4` suffix is added by
# `GitlabDiscovery` itself.
GITLAB_DEFAULT_BASE_URL = "https://gitlab.com"


class GitlabDiscovery(DiscoverySource):
    """Discover Terraform-bearing projects under one or more GitLab groups."""

    name = "gitlab"

    def __init__(
        self,
        creds: GitlabCredentials,
        group_ids: list[int] | None = None,
        base_url: str = GITLAB_DEFAULT_BASE_URL,
    ) -> None:
        # Accept either a bare host base (e.g. `https://gitlab.com`) or one
        # that already includes the `/api/v4` suffix — the previous form is
        # the canonical config value, the latter is back-compat for callers
        # that pre-built the URL.
        self._headers = {"PRIVATE-TOKEN": creds.token}
        self._base_url = base_url if base_url.rstrip("/").endswith("/api/v4") else base_url.rstrip("/") + "/api/v4"
        self._group_ids = group_ids or []

    async def discover(self) -> list[RepoMetadata]:
        return await self.list_projects_with_terraform(self._group_ids)

    async def list_projects_with_terraform(self, group_ids: list[int]) -> list[RepoMetadata]:
        if not group_ids:
            return []
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT_S,
        ) as client:
            tasks = [self._discover_group(client, gid) for gid in group_ids]
            per_group = await asyncio.gather(*tasks)

        # Deduplicate across groups by `full_name` — a project can appear under
        # both the parent group and a subgroup search.
        merged: dict[str, RepoMetadata] = {}
        for repos in per_group:
            for r in repos:
                merged[r.full_name] = r
        return list(merged.values())

    async def _discover_group(self, client: httpx.AsyncClient, group_id: int) -> list[RepoMetadata]:
        project_ids = await self._search_blobs_for_project_ids(client, group_id)
        logger.info("gitlab: group %d → %d projects with .tf files", group_id, len(project_ids))
        tasks = [self._fetch_project_metadata(client, pid) for pid in project_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[RepoMetadata] = []
        for r in results:
            if isinstance(r, RepoMetadata):
                out.append(r)
            elif isinstance(r, Exception):
                logger.warning("gitlab: skipped project (metadata fetch failed): %s", r)
        return out

    async def _search_blobs_for_project_ids(self, client: httpx.AsyncClient, group_id: int) -> list[int]:
        """Return deduplicated project IDs found via blob-search for `extension:tf`."""
        project_ids: set[int] = set()
        page = 1
        while page <= MAX_PAGES:
            resp = await client.get(
                f"/groups/{group_id}/search",
                params={
                    "scope": "blobs",
                    "search": "extension:tf",
                    "per_page": 100,
                    "page": page,
                },
            )
            if resp.status_code == 404:
                raise DiscoveryError(f"gitlab group {group_id} not found")
            if resp.status_code >= 400:
                raise DiscoveryError(
                    f"gitlab blob search failed (group={group_id}, status={resp.status_code}): {resp.text[:200]}"
                )
            batch = resp.json()
            if not batch:
                break
            for blob in batch:
                # blob shape: {"project_id": 42, "path": "main.tf", ...}
                pid = blob.get("project_id")
                if isinstance(pid, int):
                    project_ids.add(pid)
            next_page = resp.headers.get("X-Next-Page", "").strip()
            if not next_page:
                break
            page = int(next_page)
        return sorted(project_ids)

    async def _fetch_project_metadata(self, client: httpx.AsyncClient, project_id: int) -> RepoMetadata:
        """One GitLab API call → one RepoMetadata. Branches + last commit included."""
        resp = await client.get(f"/projects/{project_id}", params={"statistics": "false"})
        resp.raise_for_status()
        proj = resp.json()
        default_branch = proj.get("default_branch") or "main"

        # Last-commit lookup uses the branches API (cheaper than commits list).
        branch_resp = await client.get(f"/projects/{project_id}/repository/branches/{default_branch}")
        branch_resp.raise_for_status()
        branch = branch_resp.json()
        commit = branch["commit"]
        last_sha = commit["id"]
        last_at = _parse_iso8601(commit["committed_date"])
        # GitLab branch.commit may emit `author_name` or `committer_name`;
        # prefer author (the original writer over the cherry-picker).
        last_author = commit.get("author_name") or commit.get("committer_name") or None

        return RepoMetadata(
            host="gitlab",
            full_name=proj["path_with_namespace"],
            clone_url=proj["http_url_to_repo"],
            web_url=proj["web_url"],
            default_branch=default_branch,
            last_commit_sha=last_sha,
            last_commit_at=last_at,
            last_commit_author=last_author,
        )
