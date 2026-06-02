"""Markdown rendering for the `LocalMarkdownPublisher`.

Pure functions. Given a `RepoInventory` (or the list-of-inventories +
child-page-ID map for the overview), produce a string of Markdown. No
filesystem access here — `LocalMarkdownPublisher` owns the file I/O.

Output style is idiomatic Markdown, NOT a faithful translation of the
ADF layout. The banner is encoded as an HTML comment so it survives
common Markdown renderers without being visually noisy, AND as a
visible info-panel callout so a human reader sees the "AUTO-GENERATED"
warning."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from iac_cartographer.graph import build_mermaid
from iac_cartographer.renderer import (
    BANNER_LEAD,
    BANNER_SHA_LABEL,
    OVERVIEW_TITLE,
    format_signals,
    infer_provider_source,
    strip_attr_quotes,
)

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import RepoInventory


# A magic prefix on the SHA comment that the Markdown publisher uses to
# find the banner SHA on the next run. Kept short so it doesn't dominate
# the file header.
_SHA_COMMENT_MARKER = "iac-cartographer-sha:"


def render_child_markdown(
    inv: RepoInventory,
    *,
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None,
    max_nodes_per_graph: int = 25,
) -> str:
    """Return the full Markdown text for one repo's child page."""
    out: list[str] = []
    out.append(_banner_html_comment(sha))
    out.append("")
    out.append(_banner_callout(sha, updated_at, pipeline_url))
    out.append("")
    out.append(f"# {inv.meta.full_name}")
    out.append("")
    out.append(_repo_metadata_line(inv))
    out.append("")

    # Purpose
    out.append("## Purpose")
    out.append("")
    if inv.narrative is None:
        out.append(
            "*(Narrative summary unavailable for this run — the LLM invocation "
            "failed. Structural facts below are unaffected.)*"
        )
    else:
        out.append(inv.narrative.purpose)
    out.append("")

    if inv.narrative is not None and inv.narrative.environments:
        out.append("## Environments")
        out.append("")
        out.append(", ".join(inv.narrative.environments))
        out.append("")

    if inv.narrative is not None and inv.narrative.owning_team_guess:
        out.append("## Owning team (guess)")
        out.append("")
        out.append(inv.narrative.owning_team_guess)
        out.append("")

    if inv.narrative is not None and inv.narrative.notable_patterns:
        out.append("## Notable patterns")
        out.append("")
        out.extend(f"- {p}" for p in inv.narrative.notable_patterns)
        out.append("")

    if inv.narrative is not None and inv.narrative.key_resources_explained:
        out.append("## Key resources")
        out.append("")
        out.append("| Resource type | Why it exists |")
        out.append("|---|---|")
        out.extend(
            f"| `{e.resource_type}` | {_md_cell(e.why_it_exists)} |" for e in inv.narrative.key_resources_explained
        )
        out.append("")

    s = inv.summary

    if s.module_paths:
        out.append("## Module layout")
        out.append("")
        out.extend(f"- `{p}`" for p in s.module_paths)
        out.append("")

    if s.state_backends:
        out.append("## State backend")
        out.append("")
        out.append("| Module path | Backend | Key | Region | Safety |")
        out.append("|---|---|---|---|---|")
        for b in s.state_backends:
            key = strip_attr_quotes(b.attrs.get("key", ""))
            region = strip_attr_quotes(b.attrs.get("region", "")) or "—"
            out.append(
                f"| `{b.module_path}` | `{b.type}` | "
                f"{f'`{key}`' if key else '—'} | "
                f"{f'`{region}`' if region != '—' else '—'} | "
                f"{_md_cell(format_signals(b.signals))} |"
            )
        out.append("")

    if s.providers:
        out.append("## Providers")
        out.append("")
        out.append("| Name | Source | Version | Alias |")
        out.append("|---|---|---|---|")
        out.extend(
            f"| `{p.name}` | {p.source or infer_provider_source(p.name)} | "
            f"`{p.version or '(unpinned)'}` | {p.alias or '—'} |"
            for p in s.providers
        )
        out.append("")

    if s.modules:
        out.append("## Modules")
        out.append("")
        out.append("| Name | Source | Version |")
        out.append("|---|---|---|")
        out.extend(f"| `{m.name}` | `{m.source}` | `{m.version or '—'}` |" for m in s.modules)
        out.append("")

    if s.resource_counts_by_type:
        out.append("## Resources by type")
        out.append("")
        out.append("| Type | Count |")
        out.append("|---|---|")
        out.extend(
            f"| `{t}` | {count} |"
            for t, count in sorted(s.resource_counts_by_type.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        out.append("")

    # Mermaid resource-dependency graph(s). GitHub-flavoured Markdown
    # and GitLab Markdown both render `\`\`\`mermaid` fenced blocks
    # natively; renderers that don't (raw .md viewers, plain editors)
    # show the source as a code block — still readable.
    mermaid_chunks = build_mermaid(inv, max_nodes_per_graph=max_nodes_per_graph)
    if mermaid_chunks:
        out.append("## Resource graph")
        out.append("")
        for chunk in mermaid_chunks:
            out.append("```mermaid")
            out.append(chunk)
            out.append("```")
            out.append("")

    if s.inputs:
        out.append("## Inputs")
        out.append("")
        out.append("| Name | Type | Required | Description |")
        out.append("|---|---|---|---|")
        out.extend(
            f"| `{v.name}` | {f'`{v.type}`' if v.type else '—'} | "
            f"{'yes' if v.required else 'no'} | {_md_cell(v.description or '')} |"
            for v in s.inputs
        )
        out.append("")

    if s.outputs:
        out.append("## Outputs")
        out.append("")
        out.append("| Name | Description |")
        out.append("|---|---|")
        out.extend(f"| `{o.name}` | {_md_cell(o.description or '')} |" for o in s.outputs)
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_overview_markdown(
    inventories: list[RepoInventory],
    child_links: dict[str, str],
    *,
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None,
) -> str:
    """Return the full Markdown text for the overview / index page.

    `child_links` maps `full_name → relative-path-or-URL` for the linked
    child pages. The `LocalMarkdownPublisher` passes in relative paths
    like `repos/op-infrastructure.md` so the rendered overview is fully
    self-contained when committed to a docs repo."""
    out: list[str] = []
    out.append(_banner_html_comment(sha))
    out.append("")
    out.append(_banner_callout(sha, updated_at, pipeline_url))
    out.append("")
    out.append(f"# {OVERVIEW_TITLE}")
    out.append("")
    out.append(
        "This document is a living inventory of every Terraform / IaC repository "
        "this organisation operates, regenerated on a scheduled cadence by "
        "[iac-cartographer](https://github.com/vakaobr/iac-cartographer). "
        "Each row links to a deep-dive page; structural facts come from "
        "`terraform-docs` + an HCL parser, and the short purpose summary is "
        "written by an LLM."
    )
    out.append("")
    out.append("## Inventory")
    out.append("")
    out.append("| Repository | Host | Providers | Environments | Resources | Last commit | Purpose |")
    out.append("|---|---|---|---|---|---|---|")
    for inv in inventories:
        href = child_links.get(inv.meta.full_name)
        repo_cell = f"[{inv.meta.full_name}]({href})" if href else inv.meta.full_name
        providers = ", ".join(sorted({p.name for p in inv.summary.providers})) or "—"
        environments = ", ".join(inv.narrative.environments) if inv.narrative and inv.narrative.environments else "—"
        resources = f"{sum(inv.summary.resource_counts_by_type.values())} resources"
        last_commit = inv.meta.last_commit_at.strftime("%Y-%m-%d")
        purpose = (inv.narrative.purpose if inv.narrative else "(no narrative)")[:120]
        out.append(
            f"| {repo_cell} | {inv.meta.host} | {providers} | {environments} | "
            f"{resources} | {last_commit} | {_md_cell(purpose)} |"
        )
    out.append("")

    out.append("## At a glance")
    out.append("")
    out.append(f"- {len(inventories)} repositor{'y' if len(inventories) == 1 else 'ies'} indexed.")
    total_resources = sum(sum(inv.summary.resource_counts_by_type.values()) for inv in inventories)
    out.append(f"- {total_resources} Terraform-managed resources across all repos.")
    provider_counter: Counter[str] = Counter()
    for inv in inventories:
        for p in inv.summary.providers:
            provider_counter[p.name] += 1
    top_providers = sorted(provider_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    if top_providers:
        readable = ", ".join(f"{name} ({count} repos)" for name, count in top_providers)
        out.append(f"- Top providers: {readable}")
    out.append("")
    return "\n".join(out).rstrip() + "\n"


def extract_banner_sha(text: str) -> str | None:
    """Mirror of `renderer.extract_banner_sha` for Markdown files. Returns
    the SHA encoded in the file's leading HTML comment, or `None` if the
    file doesn't have one (treated as "changed")."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("<!--") and _SHA_COMMENT_MARKER in stripped:
            # `<!-- iac-cartographer-sha: abc123 -->`
            tail = stripped.split(_SHA_COMMENT_MARKER, 1)[1]
            token = tail.replace("-->", "").strip()
            return token or None
        # Stop scanning once we hit any non-empty line that isn't an HTML
        # comment — the banner has to be at the top.
        if stripped and not stripped.startswith("<!--"):
            return None
    return None


# ─── internals ─────────────────────────────────────────────────────────


def _banner_html_comment(sha: str) -> str:
    return f"<!-- {_SHA_COMMENT_MARKER} {sha} -->"


def _banner_callout(sha: str, updated_at: datetime, pipeline_url: str | None) -> str:
    """A visible "this page is autogenerated" notice in GitHub-flavoured
    Markdown's blockquote-callout style. Renders as a normal blockquote
    on plain Markdown viewers."""
    lines = [
        f"> **⚠️ {BANNER_LEAD}** — do not edit. Manual edits will be overwritten on the next scheduled run.",
        ">  ",
        f"> **Last updated:** `{updated_at.isoformat(timespec='seconds')}`  ",
        f"> **{BANNER_SHA_LABEL}** `{sha}`",
    ]
    if pipeline_url:
        lines.append(f"> **Pipeline:** {pipeline_url}")
    return "\n".join(lines)


def _repo_metadata_line(inv: RepoInventory) -> str:
    parts = [
        f"**Repository:** [{inv.meta.web_url}]({inv.meta.web_url})",
        f"**Default branch:** `{inv.meta.default_branch}`",
        f"**Last commit:** {inv.meta.last_commit_at.strftime('%Y-%m-%d')} (`{inv.meta.last_commit_sha[:8]}`)",
    ]
    if inv.meta.last_commit_author:
        parts.append(f"**by** {inv.meta.last_commit_author}")
    return " · ".join(parts)


def _md_cell(text: str) -> str:
    """Escape characters that would break Markdown table-cell rendering:
    pipes (cell delimiter) and newlines (row delimiter)."""
    return text.replace("|", "\\|").replace("\n", " ").strip()
