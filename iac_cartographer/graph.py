"""Mermaid resource-dependency graph generator.

Surfaces "which provider owns which resources in this repo" as a visual
group rather than buried in the resources-by-type table. Confluence
(via the native Mermaid macro) and GitHub-flavoured Markdown both
render Mermaid inline, so no headless renderer dependency.

Design — v1 (this module)
-------------------------
- One node per `ResourceRef`, labelled `<type>.<name>`.
- One node per provider, drawn with a rounded/stadium shape so the
  two node populations are visually distinguishable.
- One edge `provider → resource` for each resource. No resource→resource
  edges yet — `depends_on` / interpolation parsing is a follow-up.

Chunking
--------
A single Mermaid diagram with hundreds of nodes is unreadable. When
the resource count exceeds `max_nodes_per_graph` (default 25,
configurable via `graph.max_nodes_per_graph`), `build_mermaid` returns
multiple diagram strings — each chunk groups whole providers together,
greedy-packed to stay under the threshold. A provider whose resource
count alone exceeds the threshold ships as its own oversized chunk
(better than splitting one provider's resources across two diagrams,
which defeats the purpose).

Return shape
------------
`build_mermaid(inv, *, max_nodes_per_graph) -> list[str]`. The list is
empty for repos with no resources (renderer skips emitting any diagram).
Caller responsibility: each string is a stand-alone Mermaid `graph TD`
ready to embed inside the publisher's native code-block syntax.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iac_cartographer.models import RepoInventory, ResourceRef


# Mermaid node IDs must match `[A-Za-z_][A-Za-z0-9_]*` (digits not allowed
# as the first char). We use index-based IDs (`r0`, `r1`, `pXX`) and put
# the human-readable text in the bracketed label, which Mermaid quotes
# verbatim — that avoids any need to escape arbitrary HCL identifiers.
_NODE_LABEL_FORBIDDEN = re.compile(r'["\\\n]')


def _escape_label(text: str) -> str:
    """Quote special chars inside a Mermaid label (`"…"`).

    Mermaid's parser breaks on unescaped quotes and backslashes inside a
    bracketed label. Replace them with HTML-entity escapes — Mermaid
    re-renders entities as their original chars at display time.
    """
    return _NODE_LABEL_FORBIDDEN.sub(
        lambda m: {'"': "&quot;", "\\": "&#92;", "\n": " "}[m.group(0)],
        text,
    )


def _provider_of(resource: ResourceRef) -> str:
    """Pull a stable provider name off a `ResourceRef`.

    `resource.provider` is the explicit `provider = aws.alias` declaration
    when present; otherwise infer from the `type` prefix (`aws_s3_bucket`
    → `aws`). Matches the heuristic the existing renderer uses for the
    Providers table.
    """
    if resource.provider:
        # `provider = aws.replica` form — strip the alias for grouping.
        return resource.provider.split(".", 1)[0]
    return resource.type.split("_", 1)[0]


def _build_one(resources: list[ResourceRef]) -> str:
    """Build one Mermaid `graph TD` string for the given resource list.

    Caller has already decided which resources belong in this chunk;
    this function does no packing of its own. Resources are emitted in
    stable order (sort by `(provider, type, name)`) so the rendered
    diagram is byte-identical across runs for the same input — the
    banner-SHA still tracks page content correctly.
    """
    lines: list[str] = ["graph TD"]

    # Group resources by provider for stable per-provider blocks. We
    # don't use Mermaid `subgraph` here because rendering of nested
    # subgraphs differs across Confluence's Mermaid extension and
    # GitHub-flavoured Markdown; flat node lists + explicit edges
    # produce identical output on both.
    by_provider: dict[str, list[tuple[int, ResourceRef]]] = {}
    for idx, r in enumerate(sorted(resources, key=lambda x: (_provider_of(x), x.type, x.name))):
        by_provider.setdefault(_provider_of(r), []).append((idx, r))

    # Provider nodes first — stadium shape (`pNN(["aws"])`) to stand
    # out from the rectangle resource nodes that follow.
    provider_ids: dict[str, str] = {}
    for pi, name in enumerate(sorted(by_provider)):
        node_id = f"p{pi}"
        provider_ids[name] = node_id
        lines.append(f'  {node_id}(["{_escape_label(name)}"]):::provider')

    # Resource nodes — square brackets are the default rectangle shape.
    for name, group in by_provider.items():
        pid = provider_ids[name]
        for idx, r in group:
            rid = f"r{idx}"
            label = _escape_label(f"{r.type}.{r.name}")
            lines.append(f'  {rid}["{label}"]')
            lines.append(f"  {pid} --> {rid}")

    # CSS class for provider nodes — pale blue fill matches the
    # convention used by the existing renderer's banner panel.
    lines.append("  classDef provider fill:#e7f3ff,stroke:#0969da,color:#0a3069")
    return "\n".join(lines)


def _pack_chunks(
    by_provider: dict[str, list[ResourceRef]],
    max_nodes_per_graph: int,
) -> list[list[ResourceRef]]:
    """Greedy-pack provider groups into chunks of ≤ `max_nodes_per_graph`
    resources each. A provider whose own resource count exceeds the
    threshold ships in a chunk by itself (oversized but whole — splitting
    a single provider across two diagrams defeats the point of grouping).

    Providers are sorted by name for deterministic packing across runs.
    """
    chunks: list[list[ResourceRef]] = []
    current: list[ResourceRef] = []
    for name in sorted(by_provider):
        group = by_provider[name]
        if current and len(current) + len(group) > max_nodes_per_graph:
            chunks.append(current)
            current = []
        current.extend(group)
    if current:
        chunks.append(current)
    return chunks


def build_mermaid(inv: RepoInventory, *, max_nodes_per_graph: int = 25) -> list[str]:
    """Generate Mermaid `graph TD` diagram strings for `inv`.

    Returns an empty list when the repo declares zero resources — the
    renderer treats that as "no diagram section on the page". One
    string per chunk when chunking applies; one string total
    otherwise.
    """
    resources = list(inv.summary.resources)
    if not resources:
        return []
    if len(resources) <= max_nodes_per_graph:
        return [_build_one(resources)]

    by_provider: dict[str, list[ResourceRef]] = {}
    for r in resources:
        by_provider.setdefault(_provider_of(r), []).append(r)
    return [_build_one(chunk) for chunk in _pack_chunks(by_provider, max_nodes_per_graph)]
