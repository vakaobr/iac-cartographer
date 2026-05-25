"""Repository discovery — find every IaC repo across GitLab + GitHub.

Two clients, one orchestrator. Each client returns `list[RepoMetadata]` for
its host; the orchestrator merges, deduplicates by `full_name`, and applies
the deny-list patterns from `DiscoveryConfig.deny_repos`.

GitLab strategy: `GET /groups/{id}/search?scope=blobs&search=extension:tf`.
This returns blobs (file matches) across the group's repos including
subgroups in modern GitLab. We dedupe by `project_id` then enrich with a
project-info call. If the blob search returns suspiciously few results,
fall back to `GET /groups/{id}/projects?include_subgroups=true&per_page=100`
+ per-repo tree check.

GitHub strategy: `GET /search/code?q=org:{org}+extension:tf` for the list of
repos, then `GET /repos/{o}/{r}` for default-branch + last-commit metadata.

Both clients reuse one `httpx.AsyncClient` per host to amortize the TLS
handshake. Pagination is exhaustive but bounded by a hard `MAX_PAGES=20`
guard against runaway loops.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from iac_cartographer.constants import DiscoveryError
from iac_cartographer.models import RepoMetadata

if TYPE_CHECKING:
    from iac_cartographer.models import DiscoveryConfig, GithubCredentials, GitlabCredentials

logger = logging.getLogger("iac_cartographer.discovery")

# GitLab default — the public hosted gitlab.com. For self-hosted GitLab
# instances, set `discovery.gitlab_base_url` in the YAML config (e.g.
# `https://gitlab.your-org.example`). The `/api/v4` suffix is added by
# `GitlabDiscovery` itself.
GITLAB_DEFAULT_BASE_URL = "https://gitlab.com"
GITHUB_BASE_URL = "https://api.github.com"
MAX_PAGES = 20  # safety cap: 20 * per_page=100 = 2000 results max per call
DEFAULT_TIMEOUT_S = 30.0


# ─── GitLab ────────────────────────────────────────────────────────────────


class GitlabDiscovery:
    """Discover Terraform-bearing projects under one or more GitLab groups."""

    def __init__(self, creds: GitlabCredentials, base_url: str = GITLAB_DEFAULT_BASE_URL) -> None:
        # Accept either a bare host base (e.g. `https://gitlab.com`) or one
        # that already includes the `/api/v4` suffix — the previous form is
        # the canonical config value, the latter is back-compat for callers
        # that pre-built the URL.
        self._headers = {"PRIVATE-TOKEN": creds.token}
        self._base_url = base_url if base_url.rstrip("/").endswith("/api/v4") else base_url.rstrip("/") + "/api/v4"

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


# ─── GitHub ────────────────────────────────────────────────────────────────


class GithubDiscovery:
    """Discover Terraform-bearing repositories under one or more GitHub orgs."""

    def __init__(self, creds: GithubCredentials, base_url: str = GITHUB_BASE_URL) -> None:
        self._headers = {
            "Authorization": f"Bearer {creds.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._base_url = base_url

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


# ─── Orchestrator ──────────────────────────────────────────────────────────


def _matches_deny_pattern(full_name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(full_name, pattern) for pattern in patterns)


async def discover(
    config: DiscoveryConfig,
    gitlab_creds: GitlabCredentials,
    github_creds: GithubCredentials,
) -> list[RepoMetadata]:
    """Run both clients in parallel; merge; apply deny-list.

    Raises `DiscoveryError` if zero repos are found (almost certainly a
    config / auth issue — fail loud rather than publishing an empty page).
    """
    gitlab_client = GitlabDiscovery(gitlab_creds, base_url=config.gitlab_base_url)
    github_client = GithubDiscovery(github_creds)

    gitlab_repos, github_repos = await asyncio.gather(
        gitlab_client.list_projects_with_terraform(config.gitlab_group_ids),
        github_client.list_repos_with_terraform(config.github_orgs),
    )

    all_repos = gitlab_repos + github_repos
    if not all_repos:
        raise DiscoveryError(
            f"no repos found (gitlab_groups={config.gitlab_group_ids}, github_orgs={config.github_orgs}) "
            "— check tokens and config"
        )

    filtered = [r for r in all_repos if not _matches_deny_pattern(r.full_name, config.deny_repos)]
    logger.info(
        "discovery: %d total, %d after deny-list (%d gitlab, %d github)",
        len(all_repos),
        len(filtered),
        len(gitlab_repos),
        len(github_repos),
    )
    return filtered


# ─── Helpers ───────────────────────────────────────────────────────────────


def _parse_iso8601(value: str) -> datetime:
    """Both GitLab and GitHub return ISO-8601 strings; Python's `fromisoformat`
    on 3.12+ handles the `Z` suffix and offsets fine. We always coerce to UTC
    for downstream determinism (banner SHA computation, etc.)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _safe_get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:  # pragma: no cover
    """Walk nested dict by keys, returning default on any miss. Kept as a
    utility for follow-up phases; not used in the current module."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
    return cur
