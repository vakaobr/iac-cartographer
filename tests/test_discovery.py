"""Phase 3 tests for iac_cartographer.discovery — GitLab + GitHub clients via respx."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from iac_cartographer.constants import DiscoveryError
from iac_cartographer.discovery import (
    GITHUB_BASE_URL,
    GITLAB_DEFAULT_BASE_URL,
    GithubDiscovery,
    GitlabDiscovery,
    _matches_deny_pattern,
    _parse_iso8601,
    discover,
)
from iac_cartographer.models import (
    DiscoveryConfig,
    GithubCredentials,
    GitlabCredentials,
    RepoMetadata,
)

# ─── Helpers ──────────────────────────────────────────────────────────────


def _project_payload(project_id: int, full_name: str) -> dict[str, object]:
    return {
        "id": project_id,
        "path_with_namespace": full_name,
        "default_branch": "main",
        "http_url_to_repo": f"https://gitlab.example.com/{full_name}.git",
        "web_url": f"https://gitlab.example.com/{full_name}",
    }


def _branch_payload_gitlab(
    sha: str = "a" * 40, when: str = "2026-05-22T10:00:00Z", author: str = "alice@acme.example.com"
) -> dict[str, object]:
    return {"name": "main", "commit": {"id": sha, "committed_date": when, "author_name": author}}


def _github_tree_payload(paths: list[str], truncated: bool = False) -> dict[str, object]:
    return {
        "tree": [{"type": "blob", "path": p} for p in paths],
        "truncated": truncated,
    }


def _mock_tree(full_name: str, paths: list[str], truncated: bool = False, base: str = GITHUB_BASE_URL) -> None:
    respx.get(f"{base}/repos/{full_name}/git/trees/main").mock(
        return_value=httpx.Response(200, json=_github_tree_payload(paths, truncated=truncated))
    )


def _github_repo_payload(full_name: str) -> dict[str, object]:
    return {
        "full_name": full_name,
        "default_branch": "main",
        "clone_url": f"https://github.com/{full_name}.git",
        "html_url": f"https://github.com/{full_name}",
    }


def _branch_payload_github(
    sha: str = "b" * 40, when: str = "2026-05-22T11:00:00Z", author: str = "bob@acme.example.com"
) -> dict[str, object]:
    return {
        "name": "main",
        "commit": {
            "sha": sha,
            "commit": {
                "committer": {"date": when, "name": "GitHub"},
                "author": {"date": when, "name": author},
            },
        },
    }


# ─── _matches_deny_pattern ────────────────────────────────────────────────


def test_deny_pattern_exact_match() -> None:
    assert _matches_deny_pattern("op/devops/archived", ["op/devops/archived"])


def test_deny_pattern_glob() -> None:
    assert _matches_deny_pattern("op/foo-archived", ["op/*-archived"])
    assert not _matches_deny_pattern("op/foo-active", ["op/*-archived"])


def test_deny_pattern_empty_list_matches_nothing() -> None:
    assert not _matches_deny_pattern("anything", [])


# ─── _parse_iso8601 ──────────────────────────────────────────────────────


def test_parse_iso8601_z_suffix_yields_utc() -> None:
    dt = _parse_iso8601("2026-05-22T10:00:00Z")
    assert dt == datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    assert dt.tzinfo == UTC


def test_parse_iso8601_offset_normalizes_to_utc() -> None:
    dt = _parse_iso8601("2026-05-22T12:00:00+02:00")
    assert dt == datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)


# ─── GitlabDiscovery ─────────────────────────────────────────────────────


@respx.mock
async def test_gitlab_discovery_happy_path() -> None:
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/groups/42/search", params={"scope": "blobs"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"project_id": 1, "path": "main.tf"}, {"project_id": 2, "path": "iam.tf"}],
            headers={"X-Next-Page": ""},
        )
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1").mock(
        return_value=httpx.Response(200, json=_project_payload(1, "acme/iac/main-cluster"))
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1/repository/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_gitlab())
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/2").mock(
        return_value=httpx.Response(200, json=_project_payload(2, "op/devops/okta-management"))
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/2/repository/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_gitlab())
    )

    creds = GitlabCredentials(token="glpat-AAAA")
    client = GitlabDiscovery(creds)
    repos = await client.list_projects_with_terraform([42])
    repos_by_name = {r.full_name: r for r in repos}
    assert set(repos_by_name) == {"acme/iac/main-cluster", "op/devops/okta-management"}
    assert all(r.host == "gitlab" for r in repos)
    assert all(r.default_branch == "main" for r in repos)


@respx.mock
async def test_gitlab_discovery_pagination() -> None:
    # Page 1 → 2 blobs + X-Next-Page=2; Page 2 → 1 blob + empty next
    page1 = respx.get(
        f"{GITLAB_DEFAULT_BASE_URL}/api/v4/groups/7/search",
        params={"scope": "blobs", "search": "extension:tf", "per_page": 100, "page": 1},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"project_id": 100}, {"project_id": 100}],  # duplicates allowed
            headers={"X-Next-Page": "2"},
        )
    )
    page2 = respx.get(
        f"{GITLAB_DEFAULT_BASE_URL}/api/v4/groups/7/search",
        params={"scope": "blobs", "search": "extension:tf", "per_page": 100, "page": 2},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"project_id": 200}],
            headers={"X-Next-Page": ""},
        )
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/100").mock(
        return_value=httpx.Response(200, json=_project_payload(100, "op/x"))
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/100/repository/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_gitlab())
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/200").mock(
        return_value=httpx.Response(200, json=_project_payload(200, "op/y"))
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/200/repository/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_gitlab())
    )

    creds = GitlabCredentials(token="x")
    repos = await GitlabDiscovery(creds).list_projects_with_terraform([7])
    assert {r.full_name for r in repos} == {"op/x", "op/y"}
    assert page1.called
    assert page2.called


@respx.mock
async def test_gitlab_discovery_404_raises_discovery_error() -> None:
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/groups/999/search").mock(
        return_value=httpx.Response(404, json={"message": "Group Not Found"})
    )
    with pytest.raises(DiscoveryError, match="not found"):
        await GitlabDiscovery(GitlabCredentials(token="x")).list_projects_with_terraform([999])


@respx.mock
async def test_gitlab_discovery_skips_repos_whose_metadata_fails() -> None:
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/groups/1/search").mock(
        return_value=httpx.Response(200, json=[{"project_id": 1}, {"project_id": 2}], headers={"X-Next-Page": ""})
    )
    # Project 1 OK
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1").mock(
        return_value=httpx.Response(200, json=_project_payload(1, "op/good"))
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1/repository/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_gitlab())
    )
    # Project 2 broken
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/2").mock(return_value=httpx.Response(500))

    repos = await GitlabDiscovery(GitlabCredentials(token="x")).list_projects_with_terraform([1])
    assert [r.full_name for r in repos] == ["op/good"]


async def test_gitlab_discovery_empty_groups_returns_empty() -> None:
    repos = await GitlabDiscovery(GitlabCredentials(token="x")).list_projects_with_terraform([])
    assert repos == []


# ─── GithubDiscovery ─────────────────────────────────────────────────────


@respx.mock
async def test_github_discovery_happy_path() -> None:
    respx.get(f"{GITHUB_BASE_URL}/orgs/acme-org/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                _github_repo_payload("acme-org/runner-fleet"),
                _github_repo_payload("acme-org/database-setup"),
                _github_repo_payload("acme-org/no-tf-here"),
            ],
        )
    )
    _mock_tree("acme-org/runner-fleet", ["main.tf", "README.md"])
    _mock_tree("acme-org/database-setup", ["bastion.tf", "rds.tf"])
    _mock_tree("acme-org/no-tf-here", ["README.md", "package.json"])
    for name in ("acme-org/runner-fleet", "acme-org/database-setup"):
        respx.get(f"{GITHUB_BASE_URL}/repos/{name}").mock(
            return_value=httpx.Response(200, json=_github_repo_payload(name))
        )
        respx.get(f"{GITHUB_BASE_URL}/repos/{name}/branches/main").mock(
            return_value=httpx.Response(200, json=_branch_payload_github())
        )

    repos = await GithubDiscovery(GithubCredentials(token="ghp_")).list_repos_with_terraform(["acme-org"])
    assert {r.full_name for r in repos} == {
        "acme-org/runner-fleet",
        "acme-org/database-setup",
    }
    assert all(r.host == "github" for r in repos)


@respx.mock
async def test_github_discovery_empty_org_returns_empty() -> None:
    """Org with no repos → discovery returns empty without error."""
    respx.get(f"{GITHUB_BASE_URL}/orgs/empty/repos").mock(return_value=httpx.Response(200, json=[]))
    repos = await GithubDiscovery(GithubCredentials(token="x")).list_repos_with_terraform(["empty"])
    assert repos == []


@respx.mock
async def test_github_discovery_skips_empty_repos() -> None:
    """A repo with no commits returns 404 or 409 on the tree endpoint —
    must be silently skipped, not raise."""
    respx.get(f"{GITHUB_BASE_URL}/orgs/acme-org/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                _github_repo_payload("acme-org/empty-repo"),
                _github_repo_payload("acme-org/has-tf"),
            ],
        )
    )
    respx.get(f"{GITHUB_BASE_URL}/repos/acme-org/empty-repo/git/trees/main").mock(
        return_value=httpx.Response(409, json={"message": "Git Repository is empty."})
    )
    _mock_tree("acme-org/has-tf", ["main.tf"])
    respx.get(f"{GITHUB_BASE_URL}/repos/acme-org/has-tf").mock(
        return_value=httpx.Response(200, json=_github_repo_payload("acme-org/has-tf"))
    )
    respx.get(f"{GITHUB_BASE_URL}/repos/acme-org/has-tf/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_github())
    )
    repos = await GithubDiscovery(GithubCredentials(token="x")).list_repos_with_terraform(["acme-org"])
    assert {r.full_name for r in repos} == {"acme-org/has-tf"}


@respx.mock
async def test_github_discovery_paginates_via_link_header() -> None:
    page1 = respx.get(
        f"{GITHUB_BASE_URL}/orgs/acme-org/repos",
        params={"type": "all", "per_page": 100, "page": 1},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[_github_repo_payload("acme-org/a")],
            headers={"Link": '<...>; rel="next"'},
        )
    )
    page2 = respx.get(
        f"{GITHUB_BASE_URL}/orgs/acme-org/repos",
        params={"type": "all", "per_page": 100, "page": 2},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[_github_repo_payload("acme-org/b")],
            headers={},  # no next
        )
    )
    _mock_tree("acme-org/a", ["main.tf"])
    _mock_tree("acme-org/b", ["infra.tf"])
    for name in ("acme-org/a", "acme-org/b"):
        respx.get(f"{GITHUB_BASE_URL}/repos/{name}").mock(
            return_value=httpx.Response(200, json=_github_repo_payload(name))
        )
        respx.get(f"{GITHUB_BASE_URL}/repos/{name}/branches/main").mock(
            return_value=httpx.Response(200, json=_branch_payload_github())
        )

    repos = await GithubDiscovery(GithubCredentials(token="x")).list_repos_with_terraform(["acme-org"])
    assert {r.full_name for r in repos} == {"acme-org/a", "acme-org/b"}
    assert page1.called and page2.called


@respx.mock
async def test_github_discovery_500_raises() -> None:
    respx.get(f"{GITHUB_BASE_URL}/orgs/acme-org/repos").mock(return_value=httpx.Response(500, json={"message": "boom"}))
    with pytest.raises(DiscoveryError):
        await GithubDiscovery(GithubCredentials(token="x")).list_repos_with_terraform(["acme-org"])


@respx.mock
async def test_github_discovery_truncated_tree_without_tf_skipped() -> None:
    """Truncated tree response with no `.tf` blobs surfaced → treat as
    'no .tf' and log a warning."""
    respx.get(f"{GITHUB_BASE_URL}/orgs/acme-org/repos").mock(
        return_value=httpx.Response(200, json=[_github_repo_payload("acme-org/huge")])
    )
    respx.get(f"{GITHUB_BASE_URL}/repos/acme-org/huge/git/trees/main").mock(
        return_value=httpx.Response(200, json=_github_tree_payload(["docs/intro.md", "package.json"], truncated=True))
    )
    repos = await GithubDiscovery(GithubCredentials(token="x")).list_repos_with_terraform(["acme-org"])
    assert repos == []


@respx.mock
async def test_github_discovery_default_base_is_public_api() -> None:
    """Sanity: with no base_url override, requests hit api.github.com."""
    route = respx.get("https://api.github.com/orgs/acme-org/repos").mock(return_value=httpx.Response(200, json=[]))
    await GithubDiscovery(GithubCredentials(token="x")).list_repos_with_terraform(["acme-org"])
    assert route.called


@respx.mock
async def test_github_discovery_uses_ghes_base_url() -> None:
    """GitHub Enterprise Server: every API call goes to the configured
    `https://<host>/api/v3` base, with paths composed correctly (no double
    /api/v3, no dropped path segment)."""
    ghes = "https://ghe.example.com/api/v3"
    respx.get(f"{ghes}/orgs/acme-org/repos").mock(
        return_value=httpx.Response(200, json=[_github_repo_payload("acme-org/infra")])
    )
    _mock_tree("acme-org/infra", ["main.tf"], base=ghes)
    repo_route = respx.get(f"{ghes}/repos/acme-org/infra").mock(
        return_value=httpx.Response(200, json=_github_repo_payload("acme-org/infra"))
    )
    branch_route = respx.get(f"{ghes}/repos/acme-org/infra/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_github())
    )

    repos = await GithubDiscovery(
        GithubCredentials(token="ghs_enterprise"),
        base_url=ghes,
    ).list_repos_with_terraform(["acme-org"])

    assert {r.full_name for r in repos} == {"acme-org/infra"}
    assert repo_route.called
    assert branch_route.called
    # And nothing leaked to the public api.github.com host.
    assert all("ghe.example.com" in str(call.request.url) for call in respx.calls)


async def test_github_discovery_strips_trailing_slash_from_base_url() -> None:
    src = GithubDiscovery(GithubCredentials(token="x"), base_url="https://ghe.example.com/api/v3/")
    assert src._base_url == "https://ghe.example.com/api/v3"


# ─── discover() orchestrator ────────────────────────────────────────────


@respx.mock
async def test_discover_merges_and_applies_deny_list() -> None:
    # GitLab: 1 repo
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/groups/1/search").mock(
        return_value=httpx.Response(200, json=[{"project_id": 1}], headers={"X-Next-Page": ""})
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1").mock(
        return_value=httpx.Response(200, json=_project_payload(1, "op/devops/archived-thing"))
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1/repository/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_gitlab())
    )
    # GitHub: 1 repo
    respx.get(f"{GITHUB_BASE_URL}/orgs/acme-org/repos").mock(
        return_value=httpx.Response(200, json=[_github_repo_payload("acme-org/active")])
    )
    _mock_tree("acme-org/active", ["main.tf"])
    respx.get(f"{GITHUB_BASE_URL}/repos/acme-org/active").mock(
        return_value=httpx.Response(200, json=_github_repo_payload("acme-org/active"))
    )
    respx.get(f"{GITHUB_BASE_URL}/repos/acme-org/active/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_github())
    )

    config = DiscoveryConfig(
        gitlab_group_ids=[1],
        github_orgs=["acme-org"],
        deny_repos=["op/devops/archived-*"],
    )
    repos = await discover(
        config,
        GitlabCredentials(token="x"),
        GithubCredentials(token="y"),
    )
    # GitLab's archived repo filtered out; only the GitHub one survives.
    assert [r.full_name for r in repos] == ["acme-org/active"]


@respx.mock
async def test_discover_zero_repos_raises() -> None:
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/groups/1/search").mock(
        return_value=httpx.Response(200, json=[], headers={"X-Next-Page": ""})
    )
    respx.get(f"{GITHUB_BASE_URL}/orgs/acme-org/repos").mock(return_value=httpx.Response(200, json=[]))
    config = DiscoveryConfig(gitlab_group_ids=[1], github_orgs=["acme-org"])
    with pytest.raises(DiscoveryError, match="no repos found"):
        await discover(config, GitlabCredentials(token="x"), GithubCredentials(token="y"))


def test_repo_metadata_dataclass_shape() -> None:
    """Sanity: RepoMetadata round-trips through model_dump and back."""
    r = RepoMetadata(
        host="gitlab",
        full_name="op/x",
        clone_url="https://x.test/acme/x.git",
        web_url="https://x.test/op/x",
        default_branch="main",
        last_commit_sha="a" * 40,
        last_commit_at=datetime(2026, 5, 22, tzinfo=UTC),
        last_commit_author="alice@acme.example.com",
    )
    dumped = r.model_dump()
    assert RepoMetadata.model_validate(dumped) == r


# ─── AI-H3 hardening: last_commit_author populated by both clients ─────────


@respx.mock
async def test_gitlab_discovery_populates_last_commit_author() -> None:
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/groups/1/search").mock(
        return_value=httpx.Response(200, json=[{"project_id": 1}], headers={"X-Next-Page": ""})
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1").mock(
        return_value=httpx.Response(200, json=_project_payload(1, "op/devops/x"))
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1/repository/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_gitlab(author="anderson@acme.example.com"))
    )
    repos = await GitlabDiscovery(GitlabCredentials(token="x")).list_projects_with_terraform([1])
    assert repos[0].last_commit_author == "anderson@acme.example.com"


@respx.mock
async def test_github_discovery_populates_last_commit_author() -> None:
    respx.get(f"{GITHUB_BASE_URL}/orgs/acme-org/repos").mock(
        return_value=httpx.Response(200, json=[_github_repo_payload("acme-org/x")])
    )
    _mock_tree("acme-org/x", ["main.tf"])
    respx.get(f"{GITHUB_BASE_URL}/repos/acme-org/x").mock(
        return_value=httpx.Response(200, json=_github_repo_payload("acme-org/x"))
    )
    respx.get(f"{GITHUB_BASE_URL}/repos/acme-org/x/branches/main").mock(
        return_value=httpx.Response(200, json=_branch_payload_github(author="carol@acme.example.com"))
    )
    repos = await GithubDiscovery(GithubCredentials(token="x")).list_repos_with_terraform(["acme-org"])
    assert repos[0].last_commit_author == "carol@acme.example.com"


@respx.mock
async def test_gitlab_discovery_handles_missing_author_name() -> None:
    """GitLab API may omit author_name (e.g. orphaned commits); pipeline must
    fall back to None rather than fail."""
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/groups/1/search").mock(
        return_value=httpx.Response(200, json=[{"project_id": 1}], headers={"X-Next-Page": ""})
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1").mock(
        return_value=httpx.Response(200, json=_project_payload(1, "op/x"))
    )
    respx.get(f"{GITLAB_DEFAULT_BASE_URL}/api/v4/projects/1/repository/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={"name": "main", "commit": {"id": "a" * 40, "committed_date": "2026-05-22T10:00:00Z"}},
        )
    )
    repos = await GitlabDiscovery(GitlabCredentials(token="x")).list_projects_with_terraform([1])
    assert repos[0].last_commit_author is None
