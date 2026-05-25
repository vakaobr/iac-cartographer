"""Local HTML publisher — write the inventory to a directory on disk as
self-contained HTML files.

Use cases:
  * **Snapshot for a stakeholder** — email the index.html as an attachment,
    or zip the directory and hand it over. No server needed, no static-site
    generator step, just open in a browser.
  * **S3 + CloudFront / GitHub Pages** — upload the directory and you have
    a hosted inventory site.
  * **Audit / compliance evidence** — open in a browser, print to PDF.
    A `@media print` block in the embedded CSS tightens the layout for
    print and drops the banner background.
  * **Air-gapped** — no external CSS, no fonts, no JS. Works opened from
    a USB stick.

Layout produced under `output_dir`:

    output_dir/
    ├── index.html                            # the overview / parent page
    └── repos/
        ├── op__iac__main-cluster.html        # one child per discovered repo
        ├── op__iac__auth-service.html        # full_name slugged with "__"
        └── ...

Each file starts with a `<meta name="iac-cartographer-sha" ...>` tag
carrying the banner SHA. On the next run we read that back, compare
against the freshly-computed SHA, and skip the write when they match —
same idempotency contract as the Confluence + Markdown publishers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from iac_cartographer.publishers.base import Publisher, PublishResult
from iac_cartographer.publishers.html_renderer import (
    extract_banner_sha,
    render_child_html,
    render_overview_html,
)

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import RepoInventory


logger = logging.getLogger("iac_cartographer.publishers.html")


_CHILDREN_SUBDIR = "repos"
_OVERVIEW_FILENAME = "index.html"


class LocalHtmlPublisher(Publisher):
    """Write the inventory as self-contained HTML files under a local directory."""

    def __init__(self, output_dir: Path | str) -> None:
        self._output_dir = Path(output_dir)

    async def __aenter__(self) -> LocalHtmlPublisher:
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
            content_fn=lambda: render_child_html(inv, sha=sha, updated_at=updated_at, pipeline_url=pipeline_url),
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
        # Turn each child's page_id (an absolute / relative filesystem path
        # from publish_child) into a relative path from the overview's
        # location, so the rendered <a href="..."> works no matter where
        # the output_dir gets hosted.
        child_links: dict[str, str] = {}
        for full_name, page_id in child_page_ids.items():
            child_path = Path(page_id)
            try:
                child_links[full_name] = str(child_path.relative_to(self._output_dir))
            except ValueError:
                # `page_id` isn't under our output_dir — keep it as-is.
                child_links[full_name] = page_id
        return self._write_with_sha_check(
            path,
            sha=sha,
            content_fn=lambda: render_overview_html(
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

        Same `/` → `__` rule as the Markdown publisher — keeps the file
        layout flat so a static-site host's URL routing stays simple."""
        slug = full_name.replace("/", "__")
        return self._output_dir / _CHILDREN_SUBDIR / f"{slug}.html"

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
                logger.info("html: %s — unchanged (sha=%s); skipping write", path, sha)
                return PublishResult(page_id=str(path), action="unchanged")
            action = "updated"
        else:
            action = "created"

        content = content_fn()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("html: %s — %s (sha=%s, %d bytes)", path, action, sha, len(content))
        return PublishResult(page_id=str(path), action=action)  # type: ignore[arg-type]
