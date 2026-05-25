"""Tests for BitbucketDiscovery — auth header, pagination, metadata enrichment."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from iac_cartographer.constants import DiscoveryError
from iac_cartographer.discovery import BITBUCKET_BASE_URL, BitbucketDiscovery
from iac_cartographer.models import BitbucketCredentials


def _repo_stub(slug: str, *, mainbranch: str = "main") -> dict[str, Any]:
    full = f"acme/{slug}"
    return {
        "full_name": full,
        "mainbranch": {"name": mainbranch},
        "links": {
            "html": {"href": f"https://bitbucket.org/{full}"},
            "clone": [
                {"name": "https", "href": f"https://bitbucket.org/{full}.git"},
                {"name": "ssh", "href": f"git@bitbucket.org:{full}.git"},
            ],
        },
        "updated_on": "2026-05-22T12:34:56.000000+00:00",
    }


def _branch_payload(sha: str = "b" * 40, *, author: str | None = "Alice <a@x>") -> dict[str, Any]:
    target: dict[str, Any] = {
        "hash": sha,
        "date": "2026-05-22T12:34:56+00:00",
    }
    if author is not None:
        target["author"] = {"raw": author}
    return {"target": target}


# ─── Auth ──────────────────────────────────────────────────────────────────


def test_bearer_auth_when_access_token_set() -> None:
    creds = BitbucketCredentials(access_token="bbat-xyz")
    d = BitbucketDiscovery(creds, workspaces=[])
    assert d._headers["Authorization"] == "Bearer bbat-xyz"


def test_basic_auth_when_app_password_set() -> None:
    creds = BitbucketCredentials(username="bot", app_password="appp-123")
    d = BitbucketDiscovery(creds, workspaces=[])
    expected = base64.b64encode(b"bot:appp-123").decode("ascii")
    assert d._headers["Authorization"] == f"Basic {expected}"


def test_credentials_require_exactly_one_auth_mode() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BitbucketCredentials()  # neither set
    with pytest.raises(ValidationError):
        BitbucketCredentials(access_token="t", username="u", app_password="p")  # both
    with pytest.raises(ValidationError):
        BitbucketCredentials(username="u")  # half-basic


# ─── Discover ──────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_bitbucket_discovery_happy_path() -> None:
    respx.get(f"{BITBUCKET_BASE_URL}/2.0/repositories/acme").mock(
        return_value=httpx.Response(
            200,
            json={"values": [_repo_stub("main-cluster"), _repo_stub("auth-service")], "next": None},
        )
    )
    respx.get(f"{BITBUCKET_BASE_URL}/2.0/repositories/acme/main-cluster/refs/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload(sha="a" * 40))
    )
    respx.get(f"{BITBUCKET_BASE_URL}/2.0/repositories/acme/auth-service/refs/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload(sha="c" * 40))
    )

    d = BitbucketDiscovery(BitbucketCredentials(access_token="t"), workspaces=["acme"])
    repos = await d.discover()
    assert {r.full_name for r in repos} == {"acme/main-cluster", "acme/auth-service"}
    assert all(r.host == "bitbucket" for r in repos)
    main_cluster = next(r for r in repos if r.full_name == "acme/main-cluster")
    assert main_cluster.last_commit_sha == "a" * 40
    assert main_cluster.last_commit_at == datetime(2026, 5, 22, 12, 34, 56, tzinfo=UTC)
    assert main_cluster.last_commit_author == "Alice <a@x>"
    assert main_cluster.clone_url == "https://bitbucket.org/acme/main-cluster.git"


@respx.mock
@pytest.mark.asyncio
async def test_bitbucket_discovery_paginates_via_next_url() -> None:
    next_url = f"{BITBUCKET_BASE_URL}/2.0/repositories/acme?page=2"
    # First page request carries the initial params; second carries `page=2`.
    # respx defaults to substring-style URL matching, so anchor the first
    # mock with its exact params to keep it from swallowing the second.
    respx.get(
        f"{BITBUCKET_BASE_URL}/2.0/repositories/acme",
        params={"role": "member", "pagelen": "100"},
    ).mock(return_value=httpx.Response(200, json={"values": [_repo_stub("alpha")], "next": next_url}))
    respx.get(f"{BITBUCKET_BASE_URL}/2.0/repositories/acme", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"values": [_repo_stub("beta")], "next": None})
    )
    respx.get(f"{BITBUCKET_BASE_URL}/2.0/repositories/acme/alpha/refs/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload())
    )
    respx.get(f"{BITBUCKET_BASE_URL}/2.0/repositories/acme/beta/refs/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload())
    )

    d = BitbucketDiscovery(BitbucketCredentials(access_token="t"), workspaces=["acme"])
    repos = await d.discover()
    assert {r.full_name for r in repos} == {"acme/alpha", "acme/beta"}


@respx.mock
@pytest.mark.asyncio
async def test_bitbucket_discovery_404_raises_discovery_error() -> None:
    respx.get(f"{BITBUCKET_BASE_URL}/2.0/repositories/ghost").mock(
        return_value=httpx.Response(404, json={"error": {"message": "not found"}})
    )
    d = BitbucketDiscovery(BitbucketCredentials(access_token="t"), workspaces=["ghost"])
    with pytest.raises(DiscoveryError, match="bitbucket workspace 'ghost' not found"):
        await d.discover()


@respx.mock
@pytest.mark.asyncio
async def test_bitbucket_discovery_skips_repos_without_mainbranch() -> None:
    empty_repo = _repo_stub("empty")
    empty_repo["mainbranch"] = None
    respx.get(f"{BITBUCKET_BASE_URL}/2.0/repositories/acme").mock(
        return_value=httpx.Response(200, json={"values": [empty_repo, _repo_stub("ok")], "next": None})
    )
    respx.get(f"{BITBUCKET_BASE_URL}/2.0/repositories/acme/ok/refs/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload())
    )

    d = BitbucketDiscovery(BitbucketCredentials(access_token="t"), workspaces=["acme"])
    repos = await d.discover()
    assert [r.full_name for r in repos] == ["acme/ok"]


@pytest.mark.asyncio
async def test_bitbucket_discovery_empty_workspaces_returns_empty() -> None:
    d = BitbucketDiscovery(BitbucketCredentials(access_token="t"), workspaces=[])
    assert await d.discover() == []
