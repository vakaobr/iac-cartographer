"""Phase 4 tests for iac_cartographer.fetcher — subprocess.run mocked."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from iac_cartographer.constants import CloneError
from iac_cartographer.fetcher import _authed_clone_url, cleanup, clone
from iac_cartographer.models import RepoMetadata


def _meta(host: str = "gitlab", clone_url: str | None = None) -> RepoMetadata:
    if clone_url is None:
        clone_url = (
            "https://gitlab.example.com/acme/iac/main-cluster.git"
            if host == "gitlab"
            else "https://github.com/acme-org/runner-fleet.git"
        )
    return RepoMetadata(
        host=host,  # type: ignore[arg-type]
        full_name="acme/iac/main-cluster" if host == "gitlab" else "acme-org/runner-fleet",
        clone_url=clone_url,
        web_url=clone_url.replace(".git", "").replace("https://", "https://"),
        default_branch="main",
        last_commit_sha="a" * 40,
        last_commit_at=datetime(2026, 5, 22, tzinfo=UTC),
    )


def test_authed_clone_url_gitlab_uses_oauth2_userinfo() -> None:
    out = _authed_clone_url(
        "https://gitlab.example.com/acme/x.git",
        host="gitlab",
        gitlab_token="GL_TOKEN",
        github_token="GH_TOKEN",
    )
    assert out == "https://oauth2:GL_TOKEN@gitlab.example.com/acme/x.git"


def test_authed_clone_url_github_uses_x_access_token() -> None:
    out = _authed_clone_url(
        "https://github.com/acme-org/x.git",
        host="github",
        gitlab_token="GL_TOKEN",
        github_token="GH_TOKEN",
    )
    assert out == "https://x-access-token:GH_TOKEN@github.com/acme-org/x.git"


def test_authed_clone_url_gitea_uses_oauth2_userinfo() -> None:
    """Gitea + Forgejo accept the same `oauth2:<token>@host` shape as
    GitLab — verified against real Gitea deployments."""
    out = _authed_clone_url(
        "https://gitea.example.com/acme/x.git",
        host="gitea",
        gitlab_token="GL_TOKEN",
        github_token="GH_TOKEN",
        gitea_token="GITEA_TOKEN",
    )
    assert out == "https://oauth2:GITEA_TOKEN@gitea.example.com/acme/x.git"


def test_authed_clone_url_gitea_without_token_raises() -> None:
    """Discovery passed a `gitea` host but the operator never set up the
    secret — fail loud with a CloneError that names the secret to add."""
    from iac_cartographer.constants import CloneError

    with pytest.raises(CloneError, match=r"iac-cartographer/gitea"):
        _authed_clone_url(
            "https://gitea.example.com/acme/x.git",
            host="gitea",
            gitlab_token="GL",
            github_token="GH",
            gitea_token=None,
        )


def test_authed_clone_url_preserves_port() -> None:
    out = _authed_clone_url(
        "https://gitlab.example.com:8443/acme/x.git",
        host="gitlab",
        gitlab_token="T",
        github_token="",
    )
    assert "gitlab.example.com:8443" in out
    assert "oauth2:T@" in out


def test_clone_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("iac_cartographer.fetcher.subprocess.run", fake_run)
    path = clone(_meta(), gitlab_token="GL", github_token="GH")
    try:
        assert path.exists()
        assert path.is_dir()
    finally:
        cleanup(path)


def test_clone_non_zero_exit_raises_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_path: list[Path] = []

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        # Capture the tempdir to assert cleanup
        # The first positional arg is `cmd`; the path is at index -2 (before "str(tmp)")
        return subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="auth failed (token GL_TOKEN exposed)"
        )

    monkeypatch.setattr("iac_cartographer.fetcher.subprocess.run", fake_run)
    # Track which paths get created via tempfile so we can assert cleanup
    real_mkdtemp = __import__("tempfile").mkdtemp

    def tracking_mkdtemp(prefix: str = "", suffix: str = "", dir: str | None = None) -> str:  # noqa: A002 — `dir` matches stdlib tempfile signature
        p = real_mkdtemp(prefix=prefix, suffix=suffix, dir=dir)
        captured_path.append(Path(p))
        return p

    monkeypatch.setattr("iac_cartographer.fetcher.tempfile.mkdtemp", tracking_mkdtemp)

    with pytest.raises(CloneError, match="git clone failed"):
        clone(_meta(), gitlab_token="GL_TOKEN", github_token="GH")
    # Stderr in the error must be scrubbed
    assert captured_path
    assert not captured_path[0].exists()  # cleaned up


def test_clone_scrubs_token_from_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="error using token GL_SECRET_TOKEN"
        )

    monkeypatch.setattr("iac_cartographer.fetcher.subprocess.run", fake_run)
    with pytest.raises(CloneError) as exc_info:
        clone(_meta(), gitlab_token="GL_SECRET_TOKEN", github_token="GH")
    assert "GL_SECRET_TOKEN" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_clone_timeout_raises_clone_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="git clone", timeout=120)

    monkeypatch.setattr("iac_cartographer.fetcher.subprocess.run", fake_run)
    with pytest.raises(CloneError, match="timed out"):
        clone(_meta(), gitlab_token="GL", github_token="GH")


def test_clone_missing_git_binary_raises_clone_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kw: object) -> None:
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")

    monkeypatch.setattr("iac_cartographer.fetcher.subprocess.run", fake_run)
    with pytest.raises(CloneError, match="git binary not found"):
        clone(_meta(), gitlab_token="GL", github_token="GH")


def test_cleanup_swallows_missing_dir(tmp_path: Path) -> None:
    cleanup(tmp_path / "does-not-exist")


def test_cleanup_swallows_oserror(tmp_path: Path) -> None:
    def boom(_path: Path) -> None:
        raise OSError("permission denied")

    fake_dir = tmp_path / "to-cleanup"
    fake_dir.mkdir()
    with patch("iac_cartographer.fetcher.shutil.rmtree", side_effect=boom):
        cleanup(fake_dir)  # must not raise


def test_clone_passes_branch_and_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("iac_cartographer.fetcher.subprocess.run", fake_run)
    path = clone(_meta(), gitlab_token="GL", github_token="GH")
    try:
        cmd = captured["cmd"]
        assert isinstance(cmd, list)
        assert cmd[0] == "git"
        assert cmd[1] == "clone"
        assert "--depth=1" in cmd
        assert "--single-branch" in cmd
        assert "--branch=main" in cmd
    finally:
        cleanup(path)
