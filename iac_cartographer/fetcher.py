"""Repo fetching — shallow `git clone --depth=1` into a temp dir.

Authentication is via the HTTPS-with-token URL form — we splice the token
into the clone URL just before invoking git. The original clone URL on
`RepoMetadata` is HTTPS-without-credentials; we never log the patched URL
(the redaction filter would mask it anyway, but the deliberate split keeps
the failure path simpler to read in tracebacks).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from iac_cartographer.constants import CloneError

if TYPE_CHECKING:
    from iac_cartographer.models import RepoMetadata

logger = logging.getLogger("iac_cartographer.fetcher")

GIT_CLONE_TIMEOUT_S = 120


def _authed_clone_url(clone_url: str, host: str, gitlab_token: str, github_token: str) -> str:
    """Splice the right token into the clone URL.

    GitLab self-hosted accepts `https://oauth2:<token>@host/...`; GitHub
    accepts `https://x-access-token:<token>@github.com/...`. Either way the
    auth lives in the URL userinfo only and never escapes this function.
    """
    parsed = urlparse(clone_url)
    if host == "gitlab":
        netloc = f"oauth2:{gitlab_token}@{parsed.hostname}"
    elif host == "github":
        netloc = f"x-access-token:{github_token}@{parsed.hostname}"
    else:  # pragma: no cover — Pydantic Literal restricts to these two
        raise CloneError(f"unsupported host: {host}")
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def clone(meta: RepoMetadata, gitlab_token: str, github_token: str) -> Path:
    """Shallow-clone `meta` into a fresh temp dir; return the path.

    Caller is responsible for `cleanup(path)` in a try/finally. Raises
    `CloneError` on any git failure or timeout.
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"iac-cartographer-{meta.host}-"))
    url = _authed_clone_url(meta.clone_url, meta.host, gitlab_token, github_token)
    cmd = [
        "git",
        "clone",
        "--depth=1",
        "--single-branch",
        f"--branch={meta.default_branch}",
        url,
        str(tmp),
    ]
    # Never log `cmd` — it contains the token. Log the safe URL instead.
    logger.info("fetcher: cloning %s (%s) → %s", meta.full_name, meta.web_url, tmp)
    try:
        result = subprocess.run(  # noqa: S603 — args are constructed from validated metadata
            cmd,
            check=False,
            capture_output=True,
            timeout=GIT_CLONE_TIMEOUT_S,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        cleanup(tmp)
        raise CloneError(f"git clone timed out after {GIT_CLONE_TIMEOUT_S}s for {meta.full_name}") from exc
    except FileNotFoundError as exc:
        # `git` binary missing from the container — fatal, not per-repo
        cleanup(tmp)
        raise CloneError(f"git binary not found on PATH: {exc}") from exc

    if result.returncode != 0:
        # `result.stderr` may contain the patched URL — redact before raising
        scrubbed = (result.stderr or "").replace(gitlab_token, "***").replace(github_token, "***")
        cleanup(tmp)
        raise CloneError(f"git clone failed for {meta.full_name}: {scrubbed[:500]}")
    return tmp


def cleanup(path: Path) -> None:
    """Remove the temp dir. Best-effort — failures are logged but never raise.

    Called from `finally` blocks after each per-repo iteration.
    """
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        logger.debug("fetcher: cleaned up %s", path)
    except OSError:
        logger.warning("fetcher: failed to clean up %s", path, exc_info=True)
