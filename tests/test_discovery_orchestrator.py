"""Tests for the source-agnostic discovery orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from iac_cartographer.constants import DiscoveryError
from iac_cartographer.discovery import DiscoverySource, discover_from_sources
from iac_cartographer.models import RepoMetadata


def _meta(full_name: str, host: str = "github") -> RepoMetadata:
    return RepoMetadata(
        host=host,  # type: ignore[arg-type]
        full_name=full_name,
        clone_url=f"https://x.test/{full_name}.git",
        web_url=f"https://x.test/{full_name}",
        default_branch="main",
        last_commit_sha="a" * 40,
        last_commit_at=datetime(2026, 5, 22, tzinfo=UTC),
    )


class _StaticSource(DiscoverySource):
    """Test double — returns a hardcoded list."""

    def __init__(self, name: str, repos: list[RepoMetadata]) -> None:
        self.name = name
        self._repos = repos

    async def discover(self) -> list[RepoMetadata]:
        return self._repos


@pytest.mark.asyncio
async def test_orchestrator_merges_two_sources() -> None:
    s1 = _StaticSource("gitlab", [_meta("op/a", "gitlab")])
    s2 = _StaticSource("github", [_meta("op/b", "github")])
    out = await discover_from_sources([s1, s2], deny_repos=[])
    assert sorted(r.full_name for r in out) == ["op/a", "op/b"]


@pytest.mark.asyncio
async def test_orchestrator_first_source_wins_on_full_name_collision() -> None:
    primary = _meta("op/shared", "gitlab")
    secondary = _meta("op/shared", "github")
    s1 = _StaticSource("gitlab", [primary])
    s2 = _StaticSource("github", [secondary])
    out = await discover_from_sources([s1, s2], deny_repos=[])
    assert len(out) == 1
    assert out[0].host == "gitlab"  # first-seen wins


@pytest.mark.asyncio
async def test_orchestrator_applies_deny_list() -> None:
    s = _StaticSource(
        "gitlab",
        [_meta("op/keep"), _meta("op/archived-old"), _meta("op/examples-foo")],
    )
    out = await discover_from_sources([s], deny_repos=["op/*archived*", "op/examples-*"])
    assert [r.full_name for r in out] == ["op/keep"]


@pytest.mark.asyncio
async def test_orchestrator_raises_when_no_sources_configured() -> None:
    with pytest.raises(DiscoveryError, match="no discovery sources configured"):
        await discover_from_sources([], deny_repos=[])


@pytest.mark.asyncio
async def test_orchestrator_raises_when_sources_return_nothing() -> None:
    s1 = _StaticSource("gitlab", [])
    s2 = _StaticSource("github", [])
    with pytest.raises(DiscoveryError, match="no repos found"):
        await discover_from_sources([s1, s2], deny_repos=[])
