"""GitHub Wiki publisher — clone the wiki repo, rewrite files, push.

GitHub Wikis are backed by a git repository at
`https://github.com/<owner>/<repo>.wiki.git`. There's no REST API for
editing wiki content — GitHub deprecated that years ago. The
canonical path for programmatic updates is the git protocol:

  1. Clone the wiki repo (`--depth=1`).
  2. Write / modify Markdown files in the working tree.
  3. `git add -A` + `git commit` + `git push`.

Layout produced:

    Home.md                        # GitHub's default landing page
    acme-org__main-cluster.md      # one file per discovered repo
    acme-org__auth-service.md      # full_name slugged with "__"
    ...

GitHub Wiki resolves a file at `<slug>.md` to the wiki page named
`<slug>` — slashes in `full_name` would create wiki sub-pages with
non-obvious URLs, so we use `__` slug substitution (matches the
local-markdown publisher). The result is one clickable wiki page per
discovered repo, plus a Home page carrying the overview + a bulleted
list of every repo with cross-links.

Idempotency: same banner-SHA-in-HTML-comment shape the local
Markdown publisher uses (`<!-- iac-cartographer-sha: <hex> -->` at
the top of every `.md` file). We read the existing file's SHA on
disk and short-circuit the rewrite when it matches the freshly
computed value.

Auth: HTTPS clone with the GitHub token spliced into the URL —
`https://<token>@github.com/<owner>/<repo>.wiki.git`. Same token
that powers GitHub discovery (`iac-cartographer/github` secret).
The token needs `public_repo` (or `repo` for private repos) on the
target repository; the wiki inherits the repo's collaborator
permissions automatically.

Performance: wiki updates are git operations (clone + commit +
push), not per-page API calls. Net wall-clock impact is dominated
by the initial clone (~1s) + push (~1s). Per-repo work is just a
file write. The banner-SHA short-circuit means unchanged repos
skip even the disk write.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from iac_cartographer.constants import CartographerError
from iac_cartographer.publishers.base import Publisher, PublishResult
from iac_cartographer.publishers.markdown_renderer import (
    extract_banner_sha,
    render_child_markdown,
    render_overview_markdown,
)

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import GithubCredentials, RepoInventory


logger = logging.getLogger("iac_cartographer.publishers.github_wiki")

# GitHub Wiki's home page MUST be named `Home.md` — that's what the
# wiki sidebar links to by default and what GitHub renders when you
# click the "Wiki" tab on a repo.
_HOME_FILENAME = "Home.md"

# Commit author defaults. Override via config when running under a
# service-account that has its own author identity (e.g. GitHub
# Actions bot — `github-actions[bot]@users.noreply.github.com`).
DEFAULT_COMMIT_AUTHOR_NAME = "iac-cartographer"
DEFAULT_COMMIT_AUTHOR_EMAIL = "iac-cartographer@noreply"

# Subprocess timeout for git operations. Wiki repos are small (one
# Markdown file per repo) so clone + push should complete in
# seconds; 60s is comfortable headroom for slow networks.
_GIT_TIMEOUT_S = 60


class GitHubWikiError(CartographerError):
    """Raised when the wiki publisher can't clone, commit, or push.

    Per the `Publisher` contract this propagates to the orchestrator
    via the per-publisher try/except — it does NOT abort the entire
    run. The next run will retry the clone+push cycle from scratch.
    """


class GitHubWikiPublisher(Publisher):
    """Write the inventory as Markdown files in a GitHub Wiki repo."""

    def __init__(
        self,
        creds: GithubCredentials,
        *,
        owner: str,
        repo: str,
        commit_author_name: str = DEFAULT_COMMIT_AUTHOR_NAME,
        commit_author_email: str = DEFAULT_COMMIT_AUTHOR_EMAIL,
        max_nodes_per_graph: int = 25,
    ) -> None:
        self._token = creds.token
        self._owner = owner
        self._repo = repo
        self._commit_author_name = commit_author_name
        self._commit_author_email = commit_author_email
        self._max_nodes_per_graph = max_nodes_per_graph
        # Set in `__aenter__`; valid for the duration of the run.
        self._workdir: Path | None = None
        # Track whether anything actually changed — when false at
        # `__aexit__` we skip the commit + push entirely.
        self._any_writes = False

    async def __aenter__(self) -> GitHubWikiPublisher:
        # Clone the wiki repo into a temp dir. Shallow clone is enough
        # — we never read history, just rewrite the working tree.
        workdir = Path(tempfile.mkdtemp(prefix="iac-cartographer-wiki-"))
        try:
            self._git_clone(workdir)
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        self._workdir = workdir
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._workdir is None:
            return
        try:
            # Only commit + push when at least one publish_* call wrote
            # something. A no-op run (all repos unchanged) leaves the
            # remote untouched — no zombie commits with empty diffs.
            if self._any_writes:
                self._git_commit_and_push(self._workdir)
            else:
                logger.info("github_wiki: no files changed — skipping commit + push")
        finally:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    async def publish_child(
        self,
        inv: RepoInventory,
        *,
        sha: str,
        updated_at: datetime,
        pipeline_url: str | None,
    ) -> PublishResult:
        assert self._workdir is not None, "GitHubWikiPublisher must be used as an async context manager"
        path = self._workdir / self._slug_filename(inv.meta.full_name)
        return self._write_with_sha_check(
            path,
            sha=sha,
            content_fn=lambda: render_child_markdown(
                inv,
                sha=sha,
                updated_at=updated_at,
                pipeline_url=pipeline_url,
                max_nodes_per_graph=self._max_nodes_per_graph,
            ),
        )

    async def publish_overview(
        self,
        inventories: list[RepoInventory],
        child_page_ids: dict[str, str],
        *,
        sha: str,
        updated_at: datetime,
        pipeline_url: str | None,
    ) -> PublishResult:
        assert self._workdir is not None, "GitHubWikiPublisher must be used as an async context manager"
        path = self._workdir / _HOME_FILENAME

        # Turn the absolute child-page filesystem paths back into wiki
        # page slugs — GitHub Wiki renders `<slug>.md` as a page at
        # URL `/wiki/<slug>`. The local-markdown publisher's
        # `render_overview_markdown` expects relative file paths; we
        # give it `<slug>` (no `.md`, no directory) so the generated
        # links work as plain wiki references.
        wiki_links: dict[str, str] = {}
        for full_name, page_id in child_page_ids.items():
            child_path = Path(page_id)
            slug = child_path.stem  # strip `.md`
            wiki_links[full_name] = slug

        return self._write_with_sha_check(
            path,
            sha=sha,
            content_fn=lambda: render_overview_markdown(
                inventories,
                wiki_links,
                sha=sha,
                updated_at=updated_at,
                pipeline_url=pipeline_url,
            ),
        )

    # ─── internals ────────────────────────────────────────────────────

    def _slug_filename(self, full_name: str) -> str:
        """Same `__` slug convention the local-markdown publisher uses
        — `op/devops/grafana` → `op__devops__grafana.md`. GitHub Wiki
        renders this as a page titled `op__devops__grafana` (clickable
        from the sidebar)."""
        return full_name.replace("/", "__") + ".md"

    def _clone_url(self) -> str:
        """HTTPS URL with the token spliced in for auth. NEVER logged
        directly — the cli's `_RedactSecretsFilter` would mask it
        anyway, but we keep the patched URL out of structured logs as
        defence-in-depth."""
        return f"https://{self._token}@github.com/{self._owner}/{self._repo}.wiki.git"

    def _git_clone(self, workdir: Path) -> None:
        """Shallow-clone the wiki repo into `workdir`. The wiki may not
        exist yet (operator created the repo but didn't initialise the
        wiki) — in that case the clone fails with a clear error; the
        operator must visit github.com/<owner>/<repo>/wiki and create
        the first page to bootstrap the .wiki.git repository."""
        logger.info("github_wiki: cloning github.com/%s/%s.wiki.git", self._owner, self._repo)
        cmd = [
            "git",
            "clone",
            "--depth=1",
            self._clone_url(),
            str(workdir),
        ]
        result = subprocess.run(  # noqa: S603 — args are constructed from validated config + token
            cmd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if result.returncode != 0:
            # `result.stderr` will mention the URL — but the token is
            # part of the URL we constructed, NOT the URL we want in
            # log output. Strip the token before logging.
            sanitized_stderr = result.stderr.replace(self._token, "<TOKEN>") if self._token else result.stderr
            raise GitHubWikiError(
                f"git clone failed for {self._owner}/{self._repo}.wiki "
                f"(exit={result.returncode}): {sanitized_stderr.strip()[:500]}"
            )

    def _git_commit_and_push(self, workdir: Path) -> None:
        """Stage + commit + push every change in the working tree.

        Idempotent at the commit-message level — multiple invocations
        with the same banner-SHA-bearing files produce identical
        commits (git considers identical trees to be no-op commits and
        the push is a fast-forward of zero commits)."""
        # 1. Stage everything (handles deletes too, e.g. a repo
        #    removed from discovery should drop its wiki page).
        self._run_git(workdir, ["add", "-A"])

        # 2. Check whether the staged tree differs from HEAD; if not,
        #    we have nothing to commit and skip the push entirely.
        diff_cmd = ["git", "diff", "--cached", "--quiet"]
        diff_check = subprocess.run(  # noqa: S603 — args are static; `git` resolved via PATH (matches fetcher.py)
            diff_cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if diff_check.returncode == 0:
            logger.info("github_wiki: staged tree matches HEAD — skipping commit + push")
            return

        # 3. Commit. Configure author per-commit (not globally) so the
        #    author identity stays scoped to this checkout — avoids
        #    polluting the host's git config.
        self._run_git(
            workdir,
            [
                "-c",
                f"user.name={self._commit_author_name}",
                "-c",
                f"user.email={self._commit_author_email}",
                "commit",
                "-m",
                "iac-cartographer: update inventory",
            ],
        )

        # 4. Push. The remote URL still carries the token (set during
        #    clone), so no extra auth wiring needed.
        self._run_git(workdir, ["push"])

        logger.info("github_wiki: committed + pushed to %s/%s.wiki", self._owner, self._repo)

    def _run_git(self, workdir: Path, args: list[str]) -> None:
        """Run a git subcommand inside `workdir`. Raises GitHubWikiError
        on non-zero exit with sanitized stderr (no token leakage)."""
        cmd = ["git", *args]
        result = subprocess.run(  # noqa: S603 — args are constructed from validated config; `git` resolved via PATH (matches fetcher.py)
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if result.returncode != 0:
            sanitized = result.stderr.replace(self._token, "<TOKEN>") if self._token else result.stderr
            raise GitHubWikiError(f"git {args[0]} failed (exit={result.returncode}): {sanitized.strip()[:500]}")

    def _write_with_sha_check(
        self,
        path: Path,
        *,
        sha: str,
        content_fn,
    ) -> PublishResult:
        """Read the existing file's banner SHA (if any) and short-circuit
        the write when it matches. Otherwise write the new content and
        flag `_any_writes` so __aexit__ knows to commit + push."""
        action: str
        if path.exists():
            prior_sha = extract_banner_sha(path.read_text(encoding="utf-8"))
            if prior_sha is not None and prior_sha == sha:
                logger.info("github_wiki: %s — unchanged (sha=%s); skipping write", path.name, sha)
                # Page identity is the wiki slug (filename without .md).
                # Mirrors what the overview's `child_page_ids` consumes.
                return PublishResult(page_id=str(path), action="unchanged")
            action = "updated"
        else:
            action = "created"

        content = content_fn()
        path.write_text(content, encoding="utf-8")
        self._any_writes = True
        logger.info("github_wiki: %s — %s (sha=%s, %d bytes)", path.name, action, sha, len(content))
        return PublishResult(page_id=str(path), action=action)  # type: ignore[arg-type]
