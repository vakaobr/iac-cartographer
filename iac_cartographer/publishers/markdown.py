"""Local Markdown publisher — write the inventory to a directory on disk.

Use cases:
  * Commit the rendered output into a `docs/` directory and let your
    static-site generator (mkdocs, Hugo, Docusaurus, Jekyll, …) build
    a public docs site.
  * Run iac-cartographer in environments that can't talk to Confluence
    but where a regenerated set of files is still useful (CI artefact,
    local-dev iteration on the renderer, air-gapped audit).

Layout produced under `output_dir`:

    output_dir/
    ├── index.md                          # the overview / parent page
    └── repos/
        ├── op__iac__main-cluster.md      # one child per discovered repo
        ├── op__iac__auth-service.md      # full_name slugged with "__"
        └── ...

Each file starts with an HTML comment carrying the banner SHA. On the
next run we read that back, compare against the freshly-computed SHA,
and skip the write when they match — same idempotency contract as
the Confluence publisher.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from iac_cartographer.publishers.base import Publisher, PublishResult
from iac_cartographer.publishers.markdown_renderer import (
    extract_banner_sha,
    render_child_markdown,
    render_overview_markdown,
)

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import RepoInventory


logger = logging.getLogger("iac_cartographer.publishers.markdown")


_CHILDREN_SUBDIR = "repos"
_OVERVIEW_FILENAME = "index.md"


class LocalMarkdownPublisher(Publisher):
    """Write the inventory as Markdown files under a local directory."""

    def __init__(self, output_dir: Path | str, *, max_nodes_per_graph: int = 25) -> None:
        self._output_dir = Path(output_dir)
        self._max_nodes_per_graph = max_nodes_per_graph

    async def __aenter__(self) -> LocalMarkdownPublisher:
        # Create the directory tree eagerly so the first publish_child
        # call doesn't race on `mkdir`.
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
        path = self._output_dir / _OVERVIEW_FILENAME
        # Turn page_id (absolute or relative path from publish_child) into
        # a relative path from the overview's location, so links work no
        # matter where the output_dir gets committed.
        child_links: dict[str, str] = {}
        for full_name, page_id in child_page_ids.items():
            child_path = Path(page_id)
            try:
                child_links[full_name] = str(child_path.relative_to(self._output_dir))
            except ValueError:
                # `page_id` isn't under our output_dir — keep it as-is,
                # the caller asked for it.
                child_links[full_name] = page_id
        return self._write_with_sha_check(
            path,
            sha=sha,
            content_fn=lambda: render_overview_markdown(
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

        Real repo names look like `op/devops/grafana-resources` — slashes
        would create nested directories, which we don't want for the
        Markdown layout (the overview's relative links would break and a
        static-site generator's URL routing gets weird). Replace `/` with
        `__` so the original name is still readable in the filename."""
        slug = full_name.replace("/", "__")
        return self._output_dir / _CHILDREN_SUBDIR / f"{slug}.md"

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
                logger.info("markdown: %s — unchanged (sha=%s); skipping write", path, sha)
                return PublishResult(page_id=str(path), action="unchanged")
            action = "updated"
        else:
            action = "created"

        content = content_fn()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("markdown: %s — %s (sha=%s, %d bytes)", path, action, sha, len(content))
        return PublishResult(page_id=str(path), action=action)  # type: ignore[arg-type]
