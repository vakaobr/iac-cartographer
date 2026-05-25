"""JSON rendering for the `LocalJsonPublisher`.

Pure functions. Given a `RepoInventory` (or the list-of-inventories + child
link map for the overview), produce a JSON string. No filesystem access
here — `LocalJsonPublisher` owns the file I/O.

Design goals:
  * **Structured, machine-readable output.** Consumers (Backstage catalog
    imports, internal CMDB feeds, dashboards, Slack-bot summarisers,
    custom Terraform-drift detectors) shouldn't have to parse Markdown
    or scrape HTML.
  * **Schema-versioned.** Top-level `iac_cartographer.schema_version`
    field declares the shape. Consumers can pin to a version and tools
    can warn on drift.
  * **Banner-SHA idempotency** lives in `iac_cartographer.sha` — same
    short-circuit contract as the Markdown / HTML / Confluence
    publishers, just read out of the file's top-level JSON instead of
    a banner comment.
  * **Pretty-printed by default** so the files are git-diffable when
    committed to a repo (deterministic key ordering via Pydantic's
    `model_dump(mode='json')`).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import RepoInventory


# Top-level shape contract version. Bump when the JSON layout changes in
# a non-additive way (renamed / removed fields, restructured nesting).
# Additive changes (new optional fields) don't require a bump — consumers
# that ignore unknown keys keep working.
SCHEMA_VERSION = "1"


def render_child_json(
    inv: RepoInventory,
    *,
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None,
) -> str:
    """Return the full JSON text for one repo's child document."""
    payload: dict[str, Any] = {
        "iac_cartographer": _banner_block(sha, updated_at, pipeline_url),
        "meta": inv.meta.model_dump(mode="json"),
        "summary": inv.summary.model_dump(mode="json"),
        "narrative": inv.narrative.model_dump(mode="json") if inv.narrative else None,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def render_overview_json(
    inventories: list[RepoInventory],
    child_links: dict[str, str],
    *,
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None,
) -> str:
    """Return the full JSON text for the overview / index document.

    Includes one summary row per repo (so the overview alone is enough
    for most catalog-import use cases without fetching per-repo files)
    plus aggregate counts for dashboards."""
    repos: list[dict[str, Any]] = []
    provider_counter: Counter[str] = Counter()
    total_resources = 0

    for inv in inventories:
        repo_resources = sum(inv.summary.resource_counts_by_type.values())
        total_resources += repo_resources
        for p in inv.summary.providers:
            provider_counter[p.name] += 1

        repos.append(
            {
                "full_name": inv.meta.full_name,
                "host": inv.meta.host,
                "web_url": inv.meta.web_url,
                "default_branch": inv.meta.default_branch,
                "last_commit_sha": inv.meta.last_commit_sha,
                "last_commit_at": inv.meta.last_commit_at.isoformat(),
                "last_commit_author": inv.meta.last_commit_author,
                "providers": sorted({p.name for p in inv.summary.providers}),
                "environments": (inv.narrative.environments if inv.narrative else []),
                "owning_team_guess": (inv.narrative.owning_team_guess if inv.narrative else None),
                "resource_count": repo_resources,
                "purpose": (inv.narrative.purpose if inv.narrative else None),
                "child_document": child_links.get(inv.meta.full_name),
            }
        )

    aggregates = {
        "repo_count": len(inventories),
        "total_resources": total_resources,
        "top_providers": [
            {"name": name, "repo_count": count}
            for name, count in sorted(provider_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        ],
    }

    payload: dict[str, Any] = {
        "iac_cartographer": _banner_block(sha, updated_at, pipeline_url),
        "aggregates": aggregates,
        "repos": repos,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def extract_banner_sha(text: str) -> str | None:
    """Mirror of `renderer.extract_banner_sha` for JSON files.

    Returns the SHA encoded in the file's `iac_cartographer.sha` field,
    or `None` if the file doesn't have one (treated as "changed").
    Uses `json.loads` rather than a regex so an embedded SHA-shaped
    substring elsewhere in the document can't match accidentally."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    block = payload.get("iac_cartographer") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return None
    sha = block.get("sha")
    return sha if isinstance(sha, str) and sha else None


# ─── internals ─────────────────────────────────────────────────────────


def _banner_block(sha: str, updated_at: datetime, pipeline_url: str | None) -> dict[str, Any]:
    """Top-level metadata block shared by child + overview documents."""
    block: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sha": sha,
        "updated_at": updated_at.isoformat(timespec="seconds"),
        "generator": "iac-cartographer",
        "generator_url": "https://github.com/vakaobr/iac-cartographer",
    }
    if pipeline_url:
        block["pipeline_url"] = pipeline_url
    return block
