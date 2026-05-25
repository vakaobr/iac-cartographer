"""DiscoverySource ABC + shared helpers.

A `DiscoverySource` enumerates Terraform-bearing repositories from one
upstream backend (GitLab group, GitHub org, Bitbucket workspace, a curated
file, …) and returns them as `list[RepoMetadata]`. The orchestrator in
`discovery.orchestrator` runs every configured source concurrently, merges
the results, deduplicates by `full_name`, and applies the deny-list.

Adding a new source means subclassing `DiscoverySource` and implementing
`discover()`. Wire it into the orchestrator by adding a branch to
`_build_sources` in `iac_cartographer/cli.py`.
"""

from __future__ import annotations

import fnmatch
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iac_cartographer.models import RepoMetadata


# Shared HTTP defaults so every source uses the same timeout + pagination
# safety cap. Tuned for GitLab/GitHub's typical response times — slow
# enough to absorb a flaky API call, fast enough that a hung connection
# doesn't stall the whole run.
DEFAULT_TIMEOUT_S = 30.0
MAX_PAGES = 20  # 20 * per_page=100 = 2000 results max per paginated call


class DiscoverySource(ABC):
    """Abstract base class for repository-enumerating sources.

    Subclasses encapsulate everything backend-specific: HTTP client setup,
    auth headers, pagination, search filters, and metadata enrichment.
    They return a flat `list[RepoMetadata]` — the orchestrator handles
    cross-source dedup and deny-list filtering.

    Sources are designed to be cheap to instantiate; the actual work happens
    inside `discover()`. The orchestrator may run multiple sources
    concurrently via `asyncio.gather`."""

    #: Human-readable label used in log lines / Slack messages
    #: (`"gitlab"`, `"github"`, `"bitbucket"`, `"file:repos.yaml"`, …).
    name: str = "source"

    @abstractmethod
    async def discover(self) -> list[RepoMetadata]:
        """Enumerate every Terraform-bearing repository this source covers."""


# ─── Helpers (re-exported from discovery.__init__) ──────────────────────


def _matches_deny_pattern(full_name: str, patterns: list[str]) -> bool:
    """Return True if `full_name` matches any glob in `patterns`."""
    return any(fnmatch.fnmatch(full_name, pattern) for pattern in patterns)


def _parse_iso8601(value: str) -> datetime:
    """Parse ISO-8601 timestamps from upstream APIs into tz-aware UTC.

    GitLab and GitHub return `2026-05-22T12:34:56Z` or with explicit
    offsets; Bitbucket emits microsecond precision. Python's
    `fromisoformat` on 3.12+ handles all three, but the `Z` suffix needs
    a manual swap to `+00:00`. We always coerce the result to UTC so
    downstream determinism (banner SHA computation, etc.) is preserved.
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
