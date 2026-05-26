"""Tests for GiteaDiscovery — auth header, pagination, metadata enrichment.

Gitea and Forgejo share the same API, so a single suite covers both
platforms. Tests pin against a fake `https://gitea.example.com` base URL
and use respx to mock the listing + branch endpoints.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from iac_cartographer.constants import DiscoveryError
from iac_cartographer.discovery import GiteaDiscovery
from iac_cartographer.models import GiteaCredentials, RepoMetadata

GITEA_BASE = "https://gitea.example.com"


def _repo_stub(slug: str, *, default_branch: str = "main") -> dict[str, Any]:
    full = f"acme/{slug}"
    return {
        "full_name": full,
        "default_branch": default_branch,
        "clone_url": f"{GITEA_BASE}/{full}.git",
        "ssh_url": f"git@gitea.example.com:{full}.git",
        "html_url": f"{GITEA_BASE}/{full}",
    }


def _branch_payload(sha: str = "b" * 40, *, author_name: str | None = "Alice") -> dict[str, Any]:
    commit: dict[str, Any] = {
        "id": sha,
        "timestamp": "2026-05-22T12:34:56Z",
    }
    if author_name is not None:
        commit["author"] = {"name": author_name, "username": "alice"}
    return {"commit": commit}


# ── Auth header shape ────────────────────────────────────────────────


def test_authorization_header_uses_token_scheme() -> None:
    """Gitea expects `Authorization: token <pat>` — NOT `Bearer <pat>`.
    Mis-using Bearer is the most common operator-side mistake when
    porting a config over from GitHub."""
    creds = GiteaCredentials(token="gt-xyz")
    d = GiteaDiscovery(creds, orgs=["acme"], base_url=GITEA_BASE)
    assert d._headers["Authorization"] == "token gt-xyz"
    assert d._headers["Accept"] == "application/json"


def test_base_url_trailing_slash_is_stripped() -> None:
    """Operator-supplied base URLs often have a trailing slash; stripping
    keeps the per-call paths clean."""
    creds = GiteaCredentials(token="x")
    d = GiteaDiscovery(creds, orgs=["acme"], base_url=f"{GITEA_BASE}/")
    assert d._base_url == GITEA_BASE


# ── Empty-input short-circuit ────────────────────────────────────────


async def test_discover_returns_empty_list_when_no_orgs_configured() -> None:
    d = GiteaDiscovery(GiteaCredentials(token="x"), orgs=[], base_url=GITEA_BASE)
    result = await d.discover()
    assert result == []


# ── Happy path ───────────────────────────────────────────────────────


@respx.mock
async def test_discover_enumerates_org_and_fetches_branch_metadata() -> None:
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                _repo_stub("main-cluster"),
                _repo_stub("auth-service"),
            ],
        )
    )
    respx.get(f"{GITEA_BASE}/api/v1/repos/acme/main-cluster/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload(sha="a" * 40))
    )
    respx.get(f"{GITEA_BASE}/api/v1/repos/acme/auth-service/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload(sha="c" * 40, author_name="Bob"))
    )

    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["acme"], base_url=GITEA_BASE)
    result = await d.discover()

    assert len(result) == 2
    by_name = {r.full_name: r for r in result}
    assert by_name["acme/main-cluster"].last_commit_sha == "a" * 40
    assert by_name["acme/main-cluster"].host == "gitea"
    assert by_name["acme/main-cluster"].default_branch == "main"
    assert by_name["acme/main-cluster"].last_commit_author == "Alice"
    assert by_name["acme/auth-service"].last_commit_author == "Bob"


@respx.mock
async def test_discover_dedupes_across_orgs() -> None:
    """Same repo listed under two orgs (rare, but possible with
    cross-org forks) → keep first-seen, no duplicates."""
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos").mock(return_value=httpx.Response(200, json=[_repo_stub("dup")]))
    respx.get(f"{GITEA_BASE}/api/v1/orgs/beta/repos").mock(return_value=httpx.Response(200, json=[_repo_stub("dup")]))
    respx.get(f"{GITEA_BASE}/api/v1/repos/acme/dup/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload())
    )

    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["acme", "beta"], base_url=GITEA_BASE)
    result = await d.discover()
    assert len(result) == 1
    assert result[0].full_name == "acme/dup"


# ── Pagination ───────────────────────────────────────────────────────


@respx.mock
async def test_discover_walks_pagination_via_link_header() -> None:
    """Gitea paginates 50 per page; we follow `rel="next"` until exhausted."""
    page1 = [_repo_stub(f"repo-{i}") for i in range(50)]
    page2 = [_repo_stub("last-repo")]
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos", params={"page": 1, "limit": 50}).mock(
        return_value=httpx.Response(
            200,
            json=page1,
            headers={"Link": f'<{GITEA_BASE}/api/v1/orgs/acme/repos?page=2>; rel="next"'},
        )
    )
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos", params={"page": 2, "limit": 50}).mock(
        return_value=httpx.Response(200, json=page2)
    )
    # Branch metadata — fake all of them to a no-op-ish response.
    respx.get(url__regex=rf"^{GITEA_BASE}/api/v1/repos/acme/.+/branches/main$").mock(
        return_value=httpx.Response(200, json=_branch_payload())
    )

    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["acme"], base_url=GITEA_BASE)
    result = await d.discover()
    # 51 unique repos across both pages.
    assert len(result) == 51


# ── Error / edge-case handling ───────────────────────────────────────


@respx.mock
async def test_discover_treats_404_org_as_empty() -> None:
    """Org doesn't exist (operator deleted it without updating config) →
    warn + return empty, don't blow up the run."""
    respx.get(f"{GITEA_BASE}/api/v1/orgs/missing/repos").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["missing"], base_url=GITEA_BASE)
    result = await d.discover()
    assert result == []


@respx.mock
async def test_discover_raises_on_non_404_4xx() -> None:
    """401 / 403 / 500 are real failures — raise so the orchestrator
    can record them, not silently produce an empty list."""
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos").mock(
        return_value=httpx.Response(401, json={"message": "bad token"})
    )
    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["acme"], base_url=GITEA_BASE)
    with pytest.raises(DiscoveryError, match=r"gitea org list failed"):
        await d.discover()


@respx.mock
async def test_discover_skips_repo_when_branch_fetch_fails() -> None:
    """One bad branch fetch doesn't sink the whole org — log + skip
    that repo, keep the others."""
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos").mock(
        return_value=httpx.Response(200, json=[_repo_stub("good"), _repo_stub("broken")])
    )
    respx.get(f"{GITEA_BASE}/api/v1/repos/acme/good/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload())
    )
    respx.get(f"{GITEA_BASE}/api/v1/repos/acme/broken/branches/main").mock(
        return_value=httpx.Response(500, json={"message": "server error"})
    )

    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["acme"], base_url=GITEA_BASE)
    result = await d.discover()
    assert len(result) == 1
    assert result[0].full_name == "acme/good"


# ── RepoMetadata sanity ──────────────────────────────────────────────


@respx.mock
async def test_repo_metadata_host_field_is_gitea() -> None:
    """Confirms the new `gitea` Literal value flows through from
    discovery → RepoMetadata → downstream consumers (fetcher's
    `_authed_clone_url` dispatch, renderer's host column, etc.)."""
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos").mock(return_value=httpx.Response(200, json=[_repo_stub("only")]))
    respx.get(f"{GITEA_BASE}/api/v1/repos/acme/only/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload())
    )

    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["acme"], base_url=GITEA_BASE)
    result = await d.discover()
    assert isinstance(result[0], RepoMetadata)
    assert result[0].host == "gitea"


# ── Pagination edge cases ────────────────────────────────────────────


@respx.mock
async def test_pagination_stops_on_empty_batch_when_link_header_missing() -> None:
    """Older Gitea versions don't send a `Link` header. The publisher
    falls back to comparing batch size against the page limit — and
    an empty batch on a fresh page (operator paginating past the end)
    must terminate cleanly rather than spin until MAX_PAGES."""
    page1 = [_repo_stub(f"r-{i}") for i in range(50)]  # exactly full → ask for page 2
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos", params={"page": 1, "limit": 50}).mock(
        return_value=httpx.Response(200, json=page1)  # no Link header at all
    )
    # Page 2: empty list — terminator on the size-fallback path.
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos", params={"page": 2, "limit": 50}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(url__regex=rf"^{GITEA_BASE}/api/v1/repos/acme/.+/branches/main$").mock(
        return_value=httpx.Response(200, json=_branch_payload())
    )

    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["acme"], base_url=GITEA_BASE)
    result = await d.discover()
    # Only the 50 from page 1 — page 2's empty list short-circuited.
    assert len(result) == 50


@respx.mock
async def test_pagination_stops_when_batch_below_page_limit() -> None:
    """When Gitea returns a page that's shorter than `limit` AND no
    `rel=\"next\"` Link header, we conclude we've seen the last page
    and stop — without making a redundant call for an empty page+1."""
    # 30 < 50 (limit) → last-page heuristic kicks in.
    page1 = [_repo_stub(f"r-{i}") for i in range(30)]
    route_p1 = respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos", params={"page": 1, "limit": 50}).mock(
        return_value=httpx.Response(200, json=page1)
    )
    # If the publisher *did* request page 2, this route would intercept it.
    # Mounting it with assert_all_called=False lets us check it never fired.
    route_p2 = respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos", params={"page": 2, "limit": 50}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(url__regex=rf"^{GITEA_BASE}/api/v1/repos/acme/.+/branches/main$").mock(
        return_value=httpx.Response(200, json=_branch_payload())
    )

    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["acme"], base_url=GITEA_BASE)
    result = await d.discover()
    assert len(result) == 30
    # Page 1 hit, page 2 never requested.
    assert route_p1.called
    assert not route_p2.called


@respx.mock
async def test_pagination_raises_on_unexpected_payload_shape() -> None:
    """Gitea's contract is to return a plain list on success. An object
    payload (e.g. error wrapped in `{"errors": [...]}` returned with
    a 200) is a contract violation — surface loudly rather than
    silently producing zero repos."""
    respx.get(f"{GITEA_BASE}/api/v1/orgs/acme/repos").mock(
        return_value=httpx.Response(200, json={"errors": ["server hiccup"]})
    )
    d = GiteaDiscovery(GiteaCredentials(token="t"), orgs=["acme"], base_url=GITEA_BASE)
    with pytest.raises(DiscoveryError, match=r"non-list payload"):
        await d.discover()
