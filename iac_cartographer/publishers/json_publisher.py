"""Local JSON publisher — write the inventory as machine-readable JSON.

Use cases:
  * **Backstage catalog import.** Point a `Location` at the overview
    JSON; a small custom processor maps each `repos[]` entry to a
    catalog `Component`.
  * **Internal CMDB / inventory feed.** Cron-fetch the overview into
    a downstream system (ServiceNow, custom DB, …).
  * **Dashboards / Slack-bots.** Read `aggregates.top_providers`,
    `aggregates.repo_count`, etc. — no parsing of Markdown / HTML
    required.
  * **Custom drift detection.** Diff two runs' overview JSONs and
    surface "new repo added", "provider version moved", etc.

Layout produced under `output_dir`:

    output_dir/
    ├── index.json                            # overview / catalog feed
    └── repos/
        ├── op__iac__main-cluster.json        # one file per discovered repo
        └── ...

Each file's top-level `iac_cartographer.sha` field carries the banner
SHA — same idempotency contract as the other publishers (read on the
next run, compare against the freshly-computed SHA, skip-on-match).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from iac_cartographer.publishers.base import Publisher, PublishResult
from iac_cartographer.publishers.json_renderer import (
    extract_banner_sha,
    render_child_json,
    render_overview_json,
)

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import RepoInventory


logger = logging.getLogger("iac_cartographer.publishers.json")


_CHILDREN_SUBDIR = "repos"
_OVERVIEW_FILENAME = "index.json"


class LocalJsonPublisher(Publisher):
    """Write the inventory as JSON files under a local directory."""

    def __init__(self, output_dir: Path | str) -> None:
        self._output_dir = Path(output_dir)

    async def __aenter__(self) -> LocalJsonPublisher:
        (self._output_dir / _CHILDREN_SUBDIR).mkdir(parents=True, exist_ok=True)
        return self

    async def publish_child(
        self,
        inv: RepoInventory,
        *,
        sha: str,
        updated_at: datetime,
        pipeline_url: str | None,
    ) -> PublishResult:
        path = self._child_path(inv.meta.full_name)
        return self._write_with_sha_check(
            path,
            sha=sha,
            content_fn=lambda: render_child_json(inv, sha=sha, updated_at=updated_at, pipeline_url=pipeline_url),
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
        path = self._output_dir / _OVERVIEW_FILENAME
        # Same relative-path translation as the Markdown / HTML publishers —
        # keeps the rendered child_document pointers portable when the
        # output_dir is rsync'd / uploaded to S3 / served via a static host.
        child_links: dict[str, str] = {}
        for full_name, page_id in child_page_ids.items():
            child_path = Path(page_id)
            try:
                child_links[full_name] = str(child_path.relative_to(self._output_dir))
            except ValueError:
                child_links[full_name] = page_id
        return self._write_with_sha_check(
            path,
            sha=sha,
            content_fn=lambda: render_overview_json(
                inventories,
                child_links,
                sha=sha,
                updated_at=updated_at,
                pipeline_url=pipeline_url,
            ),
        )

    # ─── internals ────────────────────────────────────────────────────

    def _child_path(self, full_name: str) -> Path:
        """Slug the repo's `full_name` into a filesystem-safe filename.

        Same `/` → `__` rule as the Markdown / HTML publishers — keeps
        the file layout flat and the relative-path translation simple."""
        slug = full_name.replace("/", "__")
        return self._output_dir / _CHILDREN_SUBDIR / f"{slug}.json"

    def _write_with_sha_check(
        self,
        path: Path,
        *,
        sha: str,
        content_fn,
    ) -> PublishResult:
        """Read the existing file's banner SHA (if any) and short-circuit
        the write when it matches the incoming SHA. Otherwise write the
        new content and report `created` vs `updated`."""
        action: str
        if path.exists():
            prior_sha = extract_banner_sha(path.read_text(encoding="utf-8"))
            if prior_sha is not None and prior_sha == sha:
                logger.info("json: %s — unchanged (sha=%s); skipping write", path, sha)
                return PublishResult(page_id=str(path), action="unchanged")
            action = "updated"
        else:
            action = "created"

        content = content_fn()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("json: %s — %s (sha=%s, %d bytes)", path, action, sha, len(content))
        return PublishResult(page_id=str(path), action=action)  # type: ignore[arg-type]
