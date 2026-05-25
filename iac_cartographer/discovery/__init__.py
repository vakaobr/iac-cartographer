"""Repository discovery — pluggable sources, one orchestrator.

Public surface (stable):

  * `DiscoverySource`        — ABC every source extends.
  * `GitlabDiscovery`        — GitLab blob-search for `.tf` files.
  * `GithubDiscovery`        — GitHub code-search for `.tf` files.
  * `BitbucketDiscovery`     — Bitbucket Cloud workspace enumeration.
  * `FileDiscovery`          — Read a curated `RepoMetadata` list from disk.
  * `discover_from_sources`  — Orchestrator: run every source, merge, dedupe.
  * `discover`               — Legacy compatibility wrapper around the
                               orchestrator. New code should call
                               `discover_from_sources` directly.

Adding a new source: subclass `DiscoverySource`, implement `discover()`,
wire instantiation into `cli._build_sources`. The orchestrator is
source-agnostic and needs no changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from iac_cartographer.discovery.base import (
    DEFAULT_TIMEOUT_S,
    MAX_PAGES,
    DiscoverySource,
    _matches_deny_pattern,
    _parse_iso8601,
)
from iac_cartographer.discovery.bitbucket import BITBUCKET_BASE_URL, BitbucketDiscovery
from iac_cartographer.discovery.file import FileDiscovery
from iac_cartographer.discovery.github import GITHUB_BASE_URL, GithubDiscovery
from iac_cartographer.discovery.gitlab import GITLAB_DEFAULT_BASE_URL, GitlabDiscovery
from iac_cartographer.discovery.orchestrator import discover_from_sources

if TYPE_CHECKING:
    from iac_cartographer.models import (
        DiscoveryConfig,
        GithubCredentials,
        GitlabCredentials,
        RepoMetadata,
    )


async def discover(
    config: DiscoveryConfig,
    gitlab_creds: GitlabCredentials,
    github_creds: GithubCredentials,
) -> list[RepoMetadata]:
    """Legacy two-source orchestrator (GitLab + GitHub only).

    Preserved so the original public API keeps working for callers that
    haven't migrated to the source-list form. New code (and the CLI's
    own internal wiring) should build the source list explicitly and
    call `discover_from_sources` — that path supports Bitbucket and the
    file source too."""
    sources: list[DiscoverySource] = [
        GitlabDiscovery(gitlab_creds, config.gitlab_group_ids, base_url=config.gitlab_base_url),
        GithubDiscovery(github_creds, config.github_orgs),
    ]
    return await discover_from_sources(sources, config.deny_repos)


__all__ = [
    "BITBUCKET_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "GITHUB_BASE_URL",
    "GITLAB_DEFAULT_BASE_URL",
    "MAX_PAGES",
    "BitbucketDiscovery",
    "DiscoverySource",
    "FileDiscovery",
    "GithubDiscovery",
    "GitlabDiscovery",
    "_matches_deny_pattern",
    "_parse_iso8601",
    "discover",
    "discover_from_sources",
]
