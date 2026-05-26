"""Tests for the GitHub Wiki publisher.

Wiki publishing is git-based — the publisher shells out to `git clone`,
`git add`, `git commit`, `git push`. Tests mock `subprocess.run` so they
don't need network access or a real GitHub repo. The mock simulates a
clean clone (creates the workdir) and verifies that:

  * `git clone` is called with the token spliced into the URL.
  * Child publishes write `<slug>.md` files with banner-SHA at the top.
  * Overview publish writes `Home.md` with cross-links.
  * `__aexit__` commits + pushes ONLY when at least one publish_*
    actually wrote (skip otherwise).
  * Banner-SHA short-circuit avoids the rewrite.
  * Token never appears in error messages.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from iac_cartographer.models import (
    GithubCredentials,
    ProviderRef,
    RepoInventory,
    RepoMetadata,
    TerraformSummary,
)
from iac_cartographer.publishers.github_wiki import (
    GitHubWikiError,
    GitHubWikiPublisher,
)


def _inv(full_name: str = "acme/main-cluster") -> RepoInventory:
    return RepoInventory(
        meta=RepoMetadata(
            host="github",
            full_name=full_name,
            clone_url=f"https://github.com/{full_name}.git",
            web_url=f"https://github.com/{full_name}",
            default_branch="main",
            last_commit_sha="abc123",
            last_commit_at=datetime(2026, 1, 15, tzinfo=UTC),
            last_commit_author="alice",
        ),
        summary=TerraformSummary(
            providers=[ProviderRef(name="aws", source="hashicorp/aws", version=">= 5.0")],
            resource_counts_by_type={"aws_instance": 1},
        ),
        narrative=None,
    )


def _clean_clone_mock(workdir_holder: dict) -> MagicMock:
    """Build a `subprocess.run` mock that simulates a successful git clone
    by creating the target directory and recording every call.

    `workdir_holder` is mutated in-place so tests can assert against the
    workdir that the publisher allocated."""

    def _side_effect(args: list[str], **kwargs) -> subprocess.CompletedProcess:
        # The first call is `git clone --depth=1 <url> <workdir>`.
        if args[:2] == ["git", "clone"]:
            workdir = Path(args[-1])
            workdir.mkdir(parents=True, exist_ok=True)
            # Simulate `git init` so subsequent commands work — create
            # a `.git` placeholder so cwd is a "valid" repo.
            (workdir / ".git").mkdir(exist_ok=True)
            workdir_holder["path"] = workdir
        # `git diff --cached --quiet` exits 0 when tree==HEAD (no
        # changes) and 1 when there's a staged diff. The publisher
        # uses this return code to decide whether to commit. Tests
        # control this via `workdir_holder["diff_returncode"]`.
        if args[:3] == ["git", "diff", "--cached"]:
            return subprocess.CompletedProcess(
                args=args, returncode=workdir_holder.get("diff_returncode", 1), stdout="", stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    mock = MagicMock(side_effect=_side_effect)
    return mock


def _creds() -> GithubCredentials:
    return GithubCredentials(token="ghp_secret_token")


# ── clone path ────────────────────────────────────────────────────────


async def test_aenter_clones_with_token_spliced_into_url() -> None:
    workdir_holder: dict = {}
    mock_run = _clean_clone_mock(workdir_holder)
    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", mock_run):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
        await pub.__aenter__()
        # Confirm the clone URL carries the token (auth on HTTPS clone).
        clone_call = mock_run.call_args_list[0]
        cloned_url = clone_call.args[0][3]
        assert cloned_url == "https://ghp_secret_token@github.com/acme/docs.wiki.git"
        # Workdir was created.
        assert workdir_holder["path"].exists()
        await pub.__aexit__(None, None, None)


async def test_aenter_raises_when_git_clone_fails() -> None:
    """Wiki repo doesn't exist → clone fails with a non-zero exit. The
    publisher surfaces it as GitHubWikiError so the orchestrator can
    record a per-publisher failure without aborting."""

    def _failing(args: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=args,
            returncode=128,
            stdout="",
            stderr="fatal: repository 'https://ghp_secret_token@github.com/acme/missing.wiki.git/' not found",
        )

    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", side_effect=_failing):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="missing")
        with pytest.raises(GitHubWikiError, match="git clone failed"):
            await pub.__aenter__()


async def test_clone_failure_strips_token_from_error_message() -> None:
    """The token must never appear in raised exception messages — the
    sanitizer replaces it with `<TOKEN>` before raising."""

    def _failing(args: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=args,
            returncode=128,
            stdout="",
            stderr="fatal: could not authenticate with token ghp_secret_token in URL",
        )

    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", side_effect=_failing):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
        with pytest.raises(GitHubWikiError) as excinfo:
            await pub.__aenter__()

        assert "ghp_secret_token" not in str(excinfo.value)
        assert "<TOKEN>" in str(excinfo.value)


# ── publish_child ─────────────────────────────────────────────────────


async def test_publish_child_writes_slugged_markdown_file() -> None:
    workdir_holder: dict = {}
    mock_run = _clean_clone_mock(workdir_holder)
    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", mock_run):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
        await pub.__aenter__()
        try:
            result = await pub.publish_child(
                _inv("acme/main-cluster"),
                sha="deadbeef",
                updated_at=datetime(2026, 5, 26, tzinfo=UTC),
                pipeline_url=None,
            )

            assert result.action == "created"
            workdir = workdir_holder["path"]
            written = workdir / "acme__main-cluster.md"
            assert written.exists()
            content = written.read_text()
            assert "iac-cartographer-sha: deadbeef" in content
            # Should reference the underlying repo full_name somewhere
            # in the rendered Markdown.
            assert "acme/main-cluster" in content
        finally:
            await pub.__aexit__(None, None, None)


async def test_publish_child_short_circuits_when_sha_matches() -> None:
    """Pre-existing file with matching banner-SHA → no rewrite, action
    is `unchanged`, and `_any_writes` stays False so the commit + push
    is skipped at __aexit__."""
    workdir_holder: dict = {}
    mock_run = _clean_clone_mock(workdir_holder)
    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", mock_run):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
        await pub.__aenter__()
        try:
            # Pre-seed the file with a matching SHA — simulates a
            # prior run.
            workdir = workdir_holder["path"]
            slug_path = workdir / "acme__main-cluster.md"
            slug_path.write_text("<!-- iac-cartographer-sha: deadbeef -->\n\nold content\n")
            original_mtime = slug_path.stat().st_mtime

            result = await pub.publish_child(
                _inv("acme/main-cluster"),
                sha="deadbeef",
                updated_at=datetime(2026, 5, 26, tzinfo=UTC),
                pipeline_url=None,
            )

            assert result.action == "unchanged"
            # File wasn't touched.
            assert slug_path.stat().st_mtime == original_mtime
            assert slug_path.read_text() == "<!-- iac-cartographer-sha: deadbeef -->\n\nold content\n"
        finally:
            await pub.__aexit__(None, None, None)


async def test_publish_child_updates_when_sha_differs() -> None:
    workdir_holder: dict = {}
    mock_run = _clean_clone_mock(workdir_holder)
    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", mock_run):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
        await pub.__aenter__()
        try:
            workdir = workdir_holder["path"]
            slug_path = workdir / "acme__main-cluster.md"
            slug_path.write_text("<!-- iac-cartographer-sha: oldsha -->\n\nold\n")

            result = await pub.publish_child(
                _inv("acme/main-cluster"),
                sha="newsha",
                updated_at=datetime(2026, 5, 26, tzinfo=UTC),
                pipeline_url=None,
            )

            assert result.action == "updated"
            assert "iac-cartographer-sha: newsha" in slug_path.read_text()
        finally:
            await pub.__aexit__(None, None, None)


# ── publish_overview ──────────────────────────────────────────────────


async def test_publish_overview_writes_home_md_with_wiki_links() -> None:
    """Overview lands at `Home.md` (GitHub's wiki landing page). Links
    to child pages are wiki slugs (no `.md` extension, no path)."""
    workdir_holder: dict = {}
    mock_run = _clean_clone_mock(workdir_holder)
    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", mock_run):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
        await pub.__aenter__()
        try:
            inv = _inv("acme/main-cluster")
            child_result = await pub.publish_child(
                inv,
                sha="x",
                updated_at=datetime(2026, 5, 26, tzinfo=UTC),
                pipeline_url=None,
            )
            result = await pub.publish_overview(
                [inv],
                {inv.meta.full_name: child_result.page_id},
                sha="overviewsha",
                updated_at=datetime(2026, 5, 26, tzinfo=UTC),
                pipeline_url=None,
            )

            assert result.action == "created"
            workdir = workdir_holder["path"]
            home_path = workdir / "Home.md"
            content = home_path.read_text()
            assert "iac-cartographer-sha: overviewsha" in content
            assert "acme/main-cluster" in content
            # Wiki link references the slug only (no .md, no dir).
            assert "acme__main-cluster" in content
            # And NOT a file path.
            assert "repos/acme" not in content
        finally:
            await pub.__aexit__(None, None, None)


# ── commit + push gating ──────────────────────────────────────────────


async def test_aexit_skips_commit_when_no_writes_happened() -> None:
    """All children short-circuited → no commit, no push. Don't burn a
    git operation on a no-op run."""
    workdir_holder: dict = {}
    mock_run = _clean_clone_mock(workdir_holder)
    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", mock_run):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
        await pub.__aenter__()
        # No publish_* calls.
        await pub.__aexit__(None, None, None)

    # Only `git clone` ran. No `add`, no `commit`, no `push`.
    invoked = [call.args[0][1] for call in mock_run.call_args_list]
    assert "clone" in invoked
    assert "add" not in invoked
    assert "commit" not in invoked
    assert "push" not in invoked


async def test_aexit_commits_and_pushes_when_a_write_happened() -> None:
    workdir_holder: dict = {"diff_returncode": 1}  # diff exists → commit
    mock_run = _clean_clone_mock(workdir_holder)
    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", mock_run):
        pub = GitHubWikiPublisher(
            _creds(),
            owner="acme",
            repo="docs",
            commit_author_name="bot",
            commit_author_email="bot@example.com",
        )
        await pub.__aenter__()
        await pub.publish_child(
            _inv("acme/main-cluster"),
            sha="x",
            updated_at=datetime(2026, 5, 26, tzinfo=UTC),
            pipeline_url=None,
        )
        await pub.__aexit__(None, None, None)

    subcmds = [call.args[0] for call in mock_run.call_args_list]
    # add, diff, commit, push all ran.
    assert any(c[:2] == ["git", "add"] for c in subcmds)
    assert any(c[:3] == ["git", "diff", "--cached"] for c in subcmds)
    assert any("commit" in c for c in subcmds)
    assert any("push" in c for c in subcmds)
    # Commit author was configured per-invocation (not globally).
    commit_call = next(c for c in subcmds if "commit" in c)
    assert "user.name=bot" in commit_call
    assert "user.email=bot@example.com" in commit_call


async def test_aexit_skips_commit_when_diff_check_is_clean() -> None:
    """File was rewritten by publish_child, but the new contents happen
    to match what's already in the repo (e.g. all repos are unchanged
    from the wiki's perspective even though we ran the publisher).
    `git diff --cached --quiet` returns 0 → skip commit + push."""
    workdir_holder: dict = {"diff_returncode": 0}  # tree matches HEAD
    mock_run = _clean_clone_mock(workdir_holder)
    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", mock_run):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
        await pub.__aenter__()
        await pub.publish_child(
            _inv("acme/main-cluster"),
            sha="x",
            updated_at=datetime(2026, 5, 26, tzinfo=UTC),
            pipeline_url=None,
        )
        await pub.__aexit__(None, None, None)

    subcmds = [call.args[0] for call in mock_run.call_args_list]
    # We ran `add` + `diff` but NOT `commit` or `push`.
    assert any(c[:2] == ["git", "add"] for c in subcmds)
    assert any(c[:3] == ["git", "diff", "--cached"] for c in subcmds)
    assert not any("commit" in c for c in subcmds)
    assert not any("push" in c for c in subcmds)


# ── cleanup ───────────────────────────────────────────────────────────


async def test_aexit_cleans_up_workdir() -> None:
    workdir_holder: dict = {}
    mock_run = _clean_clone_mock(workdir_holder)
    with patch("iac_cartographer.publishers.github_wiki.subprocess.run", mock_run):
        pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
        await pub.__aenter__()
        workdir = workdir_holder["path"]
        assert workdir.exists()
        await pub.__aexit__(None, None, None)
        # tmp dir is gone.
        assert not workdir.exists()
        assert pub._workdir is None


# ── slug filename rule ────────────────────────────────────────────────


def test_slug_filename_replaces_slashes_with_double_underscore() -> None:
    pub = GitHubWikiPublisher(_creds(), owner="acme", repo="docs")
    assert pub._slug_filename("op/devops/grafana") == "op__devops__grafana.md"
    assert pub._slug_filename("flat-repo") == "flat-repo.md"
