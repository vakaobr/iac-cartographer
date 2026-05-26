"""Publisher backends — where the inventory ends up.

A `Publisher` takes one render of the structural facts + Bedrock narrative
for one repo (the "child" page) and the cross-cutting overview (the
"parent" page) and writes them to wherever the operator points it. Two
publishers ship by default:

  * `ConfluencePublisher` — Atlassian Confluence Cloud, via the v2 REST
    API. ADF body, banner-SHA idempotency, single parent page acts as the
    overview with children listed underneath.
  * `LocalMarkdownPublisher` — a directory on disk. One `<repo>.md` per
    child plus an `index.md` overview. Same banner-SHA short-circuit
    encoded as an HTML comment at the top of each file.

Adding a new publisher (Notion, GitHub Wiki, …) means subclassing
`Publisher` and overriding `publish_child` + `publish_overview`.
"""

from __future__ import annotations

from iac_cartographer.publishers.base import Publisher, PublishResult
from iac_cartographer.publishers.confluence import ConfluencePublisher
from iac_cartographer.publishers.github_wiki import GitHubWikiPublisher
from iac_cartographer.publishers.html import LocalHtmlPublisher
from iac_cartographer.publishers.json_publisher import LocalJsonPublisher
from iac_cartographer.publishers.markdown import LocalMarkdownPublisher
from iac_cartographer.publishers.notion import NotionPublisher

__all__ = [
    "ConfluencePublisher",
    "GitHubWikiPublisher",
    "LocalHtmlPublisher",
    "LocalJsonPublisher",
    "LocalMarkdownPublisher",
    "NotionPublisher",
    "PublishResult",
    "Publisher",
]
