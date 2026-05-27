"""HTML rendering for the `LocalHtmlPublisher`.

Pure functions. Given a `RepoInventory` (or the list-of-inventories +
child-link map for the overview), produce a string of HTML. No
filesystem access here — `LocalHtmlPublisher` owns the file I/O.

Design goals:
  * **Self-contained files.** All CSS is embedded in a `<style>` block;
    no external fonts, no JS, no `<link>` tags. The file works opened
    directly from disk (file://), uploaded to S3 + CloudFront, mailed
    as an attachment, or printed to PDF.
  * **Banner-SHA idempotency** matches the Markdown / Confluence
    publishers — a hidden `<meta name="iac-cartographer-sha" ...>` tag
    at the top of every file is what `extract_banner_sha` reads on
    the next run.
  * **Print-friendly.** Audit teams print these. A `@media print` block
    drops the banner background and tightens spacing.
  * **No external dependencies** — Python's `html.escape` for escaping;
    everything else is f-strings.
"""

from __future__ import annotations

from collections import Counter
from html import escape
from typing import TYPE_CHECKING

from iac_cartographer.renderer import (
    BANNER_LEAD,
    BANNER_SHA_LABEL,
    OVERVIEW_TITLE,
    infer_provider_source,
)

if TYPE_CHECKING:
    from datetime import datetime

    from iac_cartographer.models import RepoInventory


# The marker the publisher uses to find the embedded SHA on the next run.
# Kept identical in shape to the Markdown / Confluence variants so the
# `Publisher` contract (banner-SHA short-circuit) reads the same.
_SHA_META_NAME = "iac-cartographer-sha"

# Rendered into a provider-table cell when no alias is set. Pulled out as
# a constant so the embedded `class="muted"` HTML attribute doesn't have
# to be quote-escaped inside the f-string that builds the row.
_ALIAS_EMPTY_CELL = '<span class="muted">—</span>'

_BASE_CSS = """\
:root {
  --bg: #ffffff;
  --fg: #1f2328;
  --muted: #57606a;
  --border: #d0d7de;
  --accent: #0969da;
  --warn-bg: #fff8c5;
  --warn-fg: #6f4400;
  --warn-border: #d4a72c;
  --code-bg: #f6f8fa;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #8b949e;
    --border: #30363d;
    --accent: #2f81f7;
    --warn-bg: #3a2d04;
    --warn-fg: #f3c75d;
    --warn-border: #9e6a03;
    --code-bg: #161b22;
  }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  color: var(--fg);
  background: var(--bg);
  margin: 0;
  padding: 2rem 1.5rem 4rem;
  line-height: 1.55;
  font-size: 15px;
}
.container { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.75rem; margin: 1rem 0 .25rem; }
h2 { font-size: 1.2rem; margin-top: 2rem; border-bottom: 1px solid var(--border); padding-bottom: .25rem; }
p.meta { color: var(--muted); margin: .25rem 0 1rem; }
p.meta a { color: var(--accent); text-decoration: none; }
p.meta a:hover { text-decoration: underline; }
.banner {
  background: var(--warn-bg);
  color: var(--warn-fg);
  border: 1px solid var(--warn-border);
  border-radius: 6px;
  padding: .75rem 1rem;
  margin: 0 0 1.5rem;
  font-size: .9rem;
}
.banner strong { letter-spacing: .03em; }
.banner ul { margin: .5rem 0 0; padding-left: 1.25rem; }
.banner li { line-height: 1.5; }
code, kbd { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .87em; background: var(--code-bg); padding: 1px 4px; border-radius: 3px; }
pre { background: var(--code-bg); padding: 1rem; border-radius: 6px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; margin: .5rem 0 1rem; font-size: .93rem; }
th, td { padding: .5rem .75rem; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
th { background: var(--code-bg); font-weight: 600; }
td.muted { color: var(--muted); }
ul.bullet { padding-left: 1.25rem; margin: .5rem 0; }
ul.bullet li { margin: .15rem 0; }
.empty { color: var(--muted); font-style: italic; }
@media print {
  body { padding: 1rem; font-size: 12pt; }
  .banner { background: transparent; color: var(--muted); border-color: var(--border); }
  h2 { page-break-after: avoid; }
  table, tr { page-break-inside: avoid; }
}
"""


def render_child_html(
    inv: RepoInventory,
    *,
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None,
) -> str:
    """Return the full HTML text for one repo's child page."""
    meta = inv.meta
    s = inv.summary
    title = escape(meta.full_name)
    body_parts: list[str] = []

    body_parts.append(_render_banner(sha, updated_at, pipeline_url))
    body_parts.append(f"<h1>{title}</h1>")
    body_parts.append(_render_repo_meta_line(inv))

    # Purpose
    body_parts.append("<h2>Purpose</h2>")
    if inv.narrative is None:
        body_parts.append(
            '<p class="empty">Narrative summary unavailable for this run — the LLM invocation '
            "failed. Structural facts below are unaffected.</p>"
        )
    else:
        body_parts.append(f"<p>{escape(inv.narrative.purpose)}</p>")

    if inv.narrative is not None and inv.narrative.environments:
        body_parts.append("<h2>Environments</h2>")
        body_parts.append(f"<p>{escape(', '.join(inv.narrative.environments))}</p>")

    if inv.narrative is not None and inv.narrative.owning_team_guess:
        body_parts.append("<h2>Owning team (guess)</h2>")
        body_parts.append(f"<p>{escape(inv.narrative.owning_team_guess)}</p>")

    if inv.narrative is not None and inv.narrative.notable_patterns:
        body_parts.append("<h2>Notable patterns</h2>")
        body_parts.append('<ul class="bullet">')
        body_parts.extend(f"<li>{escape(p)}</li>" for p in inv.narrative.notable_patterns)
        body_parts.append("</ul>")

    if inv.narrative is not None and inv.narrative.key_resources_explained:
        body_parts.append("<h2>Key resources</h2>")
        body_parts.append("<table>")
        body_parts.append("<thead><tr><th>Resource type</th><th>Why it exists</th></tr></thead>")
        body_parts.append("<tbody>")
        body_parts.extend(
            f"<tr><td><code>{escape(e.resource_type)}</code></td><td>{escape(e.why_it_exists)}</td></tr>"
            for e in inv.narrative.key_resources_explained
        )
        body_parts.append("</tbody></table>")

    if s.module_paths:
        body_parts.append("<h2>Module layout</h2>")
        body_parts.append('<ul class="bullet">')
        body_parts.extend(f"<li><code>{escape(p)}</code></li>" for p in s.module_paths)
        body_parts.append("</ul>")

    if s.providers:
        body_parts.append("<h2>Providers</h2>")
        body_parts.append("<table>")
        body_parts.append("<thead><tr><th>Name</th><th>Source</th><th>Version</th><th>Alias</th></tr></thead>")
        body_parts.append("<tbody>")
        body_parts.extend(
            f"<tr><td><code>{escape(p.name)}</code></td>"
            f"<td>{escape(p.source or infer_provider_source(p.name))}</td>"
            f"<td><code>{escape(p.version or '(unpinned)')}</code></td>"
            f"<td>{escape(p.alias) if p.alias else _ALIAS_EMPTY_CELL}</td></tr>"
            for p in s.providers
        )
        body_parts.append("</tbody></table>")

    if s.modules:
        body_parts.append("<h2>Modules</h2>")
        body_parts.append("<table>")
        body_parts.append("<thead><tr><th>Name</th><th>Source</th><th>Version</th></tr></thead>")
        body_parts.append("<tbody>")
        body_parts.extend(
            f"<tr><td><code>{escape(m.name)}</code></td>"
            f"<td><code>{escape(m.source)}</code></td>"
            f"<td><code>{escape(m.version or '—')}</code></td></tr>"
            for m in s.modules
        )
        body_parts.append("</tbody></table>")

    if s.resource_counts_by_type:
        body_parts.append("<h2>Resources by type</h2>")
        body_parts.append("<table>")
        body_parts.append("<thead><tr><th>Type</th><th>Count</th></tr></thead>")
        body_parts.append("<tbody>")
        body_parts.extend(
            f"<tr><td><code>{escape(t)}</code></td><td>{count}</td></tr>"
            for t, count in sorted(s.resource_counts_by_type.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        body_parts.append("</tbody></table>")

    if s.inputs:
        body_parts.append("<h2>Inputs</h2>")
        body_parts.append("<table>")
        body_parts.append("<thead><tr><th>Name</th><th>Type</th><th>Required</th><th>Description</th></tr></thead>")
        body_parts.append("<tbody>")
        body_parts.extend(
            f"<tr><td><code>{escape(v.name)}</code></td>"
            f"<td>{f'<code>{escape(v.type)}</code>' if v.type else '—'}</td>"
            f"<td>{'yes' if v.required else 'no'}</td>"
            f"<td>{escape(v.description or '')}</td></tr>"
            for v in s.inputs
        )
        body_parts.append("</tbody></table>")

    if s.outputs:
        body_parts.append("<h2>Outputs</h2>")
        body_parts.append("<table>")
        body_parts.append("<thead><tr><th>Name</th><th>Description</th></tr></thead>")
        body_parts.append("<tbody>")
        body_parts.extend(
            f"<tr><td><code>{escape(o.name)}</code></td><td>{escape(o.description or '')}</td></tr>" for o in s.outputs
        )
        body_parts.append("</tbody></table>")

    return _wrap_document(title=title, sha=sha, body="\n".join(body_parts))


def render_overview_html(
    inventories: list[RepoInventory],
    child_links: dict[str, str],
    *,
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None,
) -> str:
    """Return the full HTML text for the overview / index page."""
    body_parts: list[str] = []
    body_parts.append(_render_banner(sha, updated_at, pipeline_url))
    body_parts.append(f"<h1>{escape(OVERVIEW_TITLE)}</h1>")
    body_parts.append(
        "<p>This document is a living inventory of every Terraform / IaC repository "
        "this organisation operates, regenerated on a scheduled cadence by "
        '<a href="https://github.com/vakaobr/iac-cartographer">iac-cartographer</a>. '
        "Each row links to a deep-dive page; structural facts come from "
        "<code>terraform-docs</code> + an HCL parser, and the short purpose "
        "summary is written by an LLM.</p>"
    )

    body_parts.append("<h2>Inventory</h2>")
    body_parts.append("<table>")
    body_parts.append(
        "<thead><tr>"
        "<th>Repository</th><th>Host</th><th>Providers</th><th>Environments</th>"
        "<th>Resources</th><th>Last commit</th><th>Purpose</th>"
        "</tr></thead>"
    )
    body_parts.append("<tbody>")
    for inv in inventories:
        href = child_links.get(inv.meta.full_name)
        repo_cell = (
            f'<a href="{escape(href, quote=True)}">{escape(inv.meta.full_name)}</a>'
            if href
            else escape(inv.meta.full_name)
        )
        providers = ", ".join(sorted({p.name for p in inv.summary.providers})) or "—"
        environments = ", ".join(inv.narrative.environments) if inv.narrative and inv.narrative.environments else "—"
        resources = f"{sum(inv.summary.resource_counts_by_type.values())} resources"
        last_commit = inv.meta.last_commit_at.strftime("%Y-%m-%d")
        purpose = (inv.narrative.purpose if inv.narrative else "(no narrative)")[:120]
        body_parts.append(
            f"<tr>"
            f"<td>{repo_cell}</td>"
            f"<td>{escape(inv.meta.host)}</td>"
            f"<td>{escape(providers)}</td>"
            f"<td>{escape(environments)}</td>"
            f"<td>{resources}</td>"
            f"<td>{last_commit}</td>"
            f"<td>{escape(purpose)}</td>"
            f"</tr>"
        )
    body_parts.append("</tbody></table>")

    body_parts.append("<h2>At a glance</h2>")
    body_parts.append('<ul class="bullet">')
    body_parts.append(f"<li>{len(inventories)} repositor{'y' if len(inventories) == 1 else 'ies'} indexed.</li>")
    total_resources = sum(sum(inv.summary.resource_counts_by_type.values()) for inv in inventories)
    body_parts.append(f"<li>{total_resources} Terraform-managed resources across all repos.</li>")
    provider_counter: Counter[str] = Counter()
    for inv in inventories:
        for p in inv.summary.providers:
            provider_counter[p.name] += 1
    top_providers = sorted(provider_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    if top_providers:
        readable = ", ".join(f"{escape(name)} ({count} repos)" for name, count in top_providers)
        body_parts.append(f"<li>Top providers: {readable}</li>")
    body_parts.append("</ul>")

    return _wrap_document(title=OVERVIEW_TITLE, sha=sha, body="\n".join(body_parts))


def extract_banner_sha(text: str) -> str | None:
    """Mirror of `renderer.extract_banner_sha` for HTML files. Returns the
    SHA encoded in the document's `<meta name="iac-cartographer-sha" ...>`
    tag, or `None` if the file doesn't have one (treated as "changed").

    Reads only the first ~1 KB of the document since the meta lives in
    the head; cheaper than parsing the whole tree for a hot path
    (every per-repo publish does an existence check + SHA comparison)."""
    # `name="iac-cartographer-sha" content="abc123"` — order-tolerant.
    head = text[:1024]
    marker = f'name="{_SHA_META_NAME}"'
    idx = head.find(marker)
    if idx == -1:
        return None
    rest = head[idx:]
    content_idx = rest.find('content="')
    if content_idx == -1:
        return None
    start = content_idx + len('content="')
    end = rest.find('"', start)
    if end == -1:
        return None
    sha = rest[start:end].strip()
    return sha or None


# ─── internals ─────────────────────────────────────────────────────────


def _wrap_document(*, title: str, sha: str, body: str) -> str:
    """Wrap the section markup in the full HTML5 document scaffold.

    The SHA goes into both a `<meta>` (machine-readable, for the
    publisher's idempotency check) and an HTML comment (a survival
    backup if the meta tag ever moves)."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f'<meta name="{_SHA_META_NAME}" content="{escape(sha, quote=True)}">\n'
        f"<!-- iac-cartographer-sha: {escape(sha)} -->\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)} — iac-cartographer</title>\n"
        f"<style>{_BASE_CSS}</style>\n"
        "</head>\n"
        '<body>\n<div class="container">\n'
        f"{body}\n"
        "</div>\n</body>\n</html>\n"
    )


def _render_banner(sha: str, updated_at: datetime, pipeline_url: str | None) -> str:
    items = [
        f"<li><strong>Last updated:</strong> <code>{escape(updated_at.isoformat(timespec='seconds'))}</code></li>",
        f"<li><strong>{escape(BANNER_SHA_LABEL)}</strong> <code>{escape(sha)}</code></li>",
    ]
    if pipeline_url:
        items.append(
            f'<li><strong>Pipeline:</strong> <a href="{escape(pipeline_url, quote=True)}">{escape(pipeline_url)}</a></li>'
        )
    return (
        '<div class="banner" role="note">\n'
        f"<strong>⚠ {escape(BANNER_LEAD)}</strong> — do not edit. Manual edits will be overwritten on the next scheduled run.\n"
        f"<ul>{''.join(items)}</ul>\n"
        "</div>"
    )


def _render_repo_meta_line(inv: RepoInventory) -> str:
    meta = inv.meta
    parts = [
        f'<strong>Repository:</strong> <a href="{escape(meta.web_url, quote=True)}">{escape(meta.web_url)}</a>',
        f"<strong>Default branch:</strong> <code>{escape(meta.default_branch)}</code>",
        (
            f"<strong>Last commit:</strong> {meta.last_commit_at.strftime('%Y-%m-%d')} "
            f"(<code>{escape(meta.last_commit_sha[:8])}</code>)"
        ),
    ]
    if meta.last_commit_author:
        parts.append(f"<strong>by</strong> {escape(meta.last_commit_author)}")
    return '<p class="meta">' + " · ".join(parts) + "</p>"
