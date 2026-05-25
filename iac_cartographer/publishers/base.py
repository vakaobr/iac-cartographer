"""Publisher ABC + result dataclass.

A `Publisher` encapsulates everything backend-specific about getting an
inventory in front of human readers: rendering (Markdown vs ADF vs HTML),
identity (file path vs page ID vs row UUID), idempotency (banner SHA
extraction strategy), and transport (file write vs HTTPS PUT).

The orchestrator in `cli.py` interacts with publishers through three
hooks only:

  1. `async with publisher:` — `__aenter__` runs any preflight that
     needs network (resolve space IDs, ensure the output dir exists,
     check Confluence parent reachability, …). `__aexit__` runs any
     teardown (close httpx clients, flush index files, …).
  2. `await publisher.publish_child(inv, ...)` — once per repo. Returns
     a `PublishResult` whose `page_id` the overview can later link to.
  3. `await publisher.publish_overview(...)` — once per run, after all
     children. Receives the map of `full_name → page_id` so it can build
     cross-links.

Per-call exceptions (network, auth, persistent 409) should raise
`CartographerError` subclasses so the orchestrator can record them
per-repo without aborting the whole run."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import RepoInventory


PublishAction = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class PublishResult:
    """Normalised return shape every publisher hands back to the orchestrator.

    `page_id` is the publisher's notion of a stable identifier:

      * `ConfluencePublisher` returns the numeric Confluence page ID.
      * `LocalMarkdownPublisher` returns the on-disk file path as a string.

    The orchestrator only uses it to (a) link child→parent in the overview
    page and (b) report it in the run summary; it never parses it.

    `action` lets the orchestrator count `pages_updated` vs `unchanged` for
    its Slack outcome message. `unchanged` is the banner-SHA short-circuit
    path — same content, no write performed."""

    page_id: str
    action: PublishAction


class Publisher(ABC):
    """Abstract base class for everything that writes the inventory."""

    async def __aenter__(self) -> Publisher:
        """Optional preflight. Subclasses override when they need to open
        connections, validate config against the live destination, etc.
        Default is a no-op for publishers that work fully offline."""
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Optional teardown. Default is a no-op."""
        return

    @abstractmethod
    async def publish_child(
        self,
        inv: RepoInventory,
        *,
        sha: str,
        updated_at: datetime,
        pipeline_url: str | None,
    ) -> PublishResult:
        """Render and publish one repo's deep-dive page.

        `sha` is the banner SHA the orchestrator computed from `inv`; the
        publisher embeds it in the output so the next run can short-circuit
        when nothing has changed."""

    @abstractmethod
    async def publish_overview(
        self,
        inventories: list[RepoInventory],
        child_page_ids: dict[str, str],
        *,
        sha: str,
        updated_at: datetime,
        pipeline_url: str | None,
    ) -> PublishResult:
        """Render and publish the overview page that lists every repo.

        `child_page_ids` is `RepoInventory.meta.full_name → PublishResult.page_id`
        from this run's `publish_child` calls. Implementations use it to
        build cross-links in the overview's table."""
