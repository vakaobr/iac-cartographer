"""Orchestrator — run every configured `DiscoverySource` and merge results.

Concurrency: one `asyncio.gather` over every source. Each source owns its
own HTTP client (or in the file source's case, no client at all), so they
don't share connection pools.

Dedup: by `RepoMetadata.full_name`. When two sources return the same
`full_name` (e.g. the same repo mirrored to multiple hosts, or a curated
file entry that overlaps with VCS discovery), the first-seen wins.
Source order in the input list therefore determines precedence.

Deny-list: glob patterns against `full_name`, applied uniformly to all
sources after the merge.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from iac_cartographer.constants import DiscoveryError
from iac_cartographer.discovery.base import _matches_deny_pattern

if TYPE_CHECKING:
    from iac_cartographer.discovery.base import DiscoverySource
    from iac_cartographer.models import RepoMetadata

logger = logging.getLogger("iac_cartographer.discovery.orchestrator")


async def discover_from_sources(
    sources: list[DiscoverySource],
    deny_repos: list[str],
) -> list[RepoMetadata]:
    """Run every source, merge, dedupe, apply deny-list.

    Raises `DiscoveryError` if the merged result is empty before the
    deny-list runs — almost certainly a config / auth problem. We fail
    loud rather than silently publishing an empty overview page."""
    if not sources:
        raise DiscoveryError(
            "no discovery sources configured — set at least one of "
            "discovery.gitlab_group_ids, discovery.github_orgs, "
            "discovery.bitbucket_workspaces, or discovery.repos_file"
        )

    per_source = await asyncio.gather(*(s.discover() for s in sources))

    # Dedupe by full_name, preserving source order (first wins).
    merged: dict[str, RepoMetadata] = {}
    counts: dict[str, int] = {}
    for src, repos in zip(sources, per_source, strict=True):
        counts[src.name] = len(repos)
        for r in repos:
            merged.setdefault(r.full_name, r)
    all_repos = list(merged.values())

    if not all_repos:
        raise DiscoveryError(f"no repos found across sources ({_format_counts(counts)}) — check tokens and config")

    filtered = [r for r in all_repos if not _matches_deny_pattern(r.full_name, deny_repos)]
    logger.info(
        "discovery: %d total, %d after deny-list (%s)",
        len(all_repos),
        len(filtered),
        _format_counts(counts),
    )
    return filtered


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name}={n}" for name, n in counts.items()) or "none"
