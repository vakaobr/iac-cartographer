"""ADF (Atlassian Document Format) renderer.

Produces two kinds of pages:

  * Overview page — banner + 8-column inventory table + cross-cutting summary.
    Each table row links to its child page.
  * Child page (one per repo) — banner + purpose narrative + environments +
    notable patterns + structured tables (providers / modules / resources by
    type / inputs / outputs).

ADF is the only representation we use; markdown→storage conversion mangles
tables (see workspace memory). Output is plain Python dicts; the Confluence
client `json.dumps()`-es them into the `value` field of the v2 PUT body.

Banner-as-state (ADR-007) lives at the top of every page. `extract_banner_sha`
is the inverse — it scans an existing ADF document and returns the prior
`Source SHA` so the orchestrator can short-circuit unchanged pages.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from iac_cartographer.models import RepoInventory

logger = logging.getLogger("iac_cartographer.renderer")

OVERVIEW_TITLE = "Terraform/IaC Inventory (auto-generated)"
BANNER_LEAD = "AUTO-GENERATED"
BANNER_SHA_LABEL = "Source SHA:"


# ─── Provider source placeholders (no terraform init) ──────────────────
#
# terraform-docs only populates `ProviderRef.source` when the repo
# explicitly declares `terraform { required_providers { x = { source = … } } }`.
# Many of our repos write the simpler `provider "x" {}` form without that
# block, leaving the table cell blank. We do NOT run `terraform init` to
# resolve the real source (15-20 min added to every weekly run, hundreds of
# MB of plugin downloads, plus backend-init / auth headaches — see chat
# 2026-05-25); instead we annotate the cell with what the repo SHOULD be
# declaring, plus a "(not declared)" tag so it's unambiguous that the
# value came from this lookup, not from the repo's HCL.
#
# Honesty note: modern Terraform (>= 0.14) requires `required_providers`
# for any non-Hashicorp namespace and will fail at `terraform init` if a
# bare `provider "cloudflare" {}` block isn't backed by an explicit
# `source = "cloudflare/cloudflare"`. The "(not declared)" marker is
# therefore a real fix-it signal for the source repo, not a cosmetic
# fallback. The pre-Hashicorp-namespace-fallback rule (look up
# `hashicorp/<name>`) is deprecated but still implicit for *some* legacy
# Hashicorp-owned providers, so for those names the canonical value we
# show here is also what plain Terraform would resolve.
#
# Maintenance: when a new provider shows up in our IaC, add it to the
# right block. Curated, not dynamic, by design — see ADR note.
_KNOWN_PROVIDER_SOURCES: dict[str, str] = {
    # ── Hashicorp-owned (the implicit fallback name resolves correctly) ──
    "aws": "hashicorp/aws",
    "azurerm": "hashicorp/azurerm",
    "google": "hashicorp/google",
    "google-beta": "hashicorp/google-beta",
    "kubernetes": "hashicorp/kubernetes",
    "helm": "hashicorp/helm",
    "vault": "hashicorp/vault",
    "consul": "hashicorp/consul",
    "nomad": "hashicorp/nomad",
    "vsphere": "hashicorp/vsphere",
    "random": "hashicorp/random",
    "null": "hashicorp/null",
    "archive": "hashicorp/archive",
    "time": "hashicorp/time",
    "local": "hashicorp/local",
    "tls": "hashicorp/tls",
    "http": "hashicorp/http",
    "external": "hashicorp/external",
    "dns": "hashicorp/dns",
    "template": "hashicorp/template",
    "cloudinit": "hashicorp/cloudinit",
    # ── Vendor-owned (declaration in required_providers is MANDATORY for
    #    modern Terraform — bare `provider "x" {}` will fail init) ─────
    "hcloud": "hetznercloud/hcloud",
    "cloudflare": "cloudflare/cloudflare",
    "gitlab": "gitlabhq/gitlab",
    "github": "integrations/github",
    "datadog": "DataDog/datadog",
    "okta": "okta/okta",
    "auth0": "auth0/auth0",
    "hetznerdns": "timohirt/hetznerdns",
    "grafana": "grafana/grafana",
}


def infer_provider_source(name: str) -> str:
    """Placeholder string for a provider whose `source` is empty.

    Format:
      * Provider name found in `_KNOWN_PROVIDER_SOURCES`:
          `"<canonical> (not declared)"`
        — communicates both what the repo SHOULD be declaring and that the
        declaration is currently absent.
      * Provider name not in the map:
          `"(not declared — unknown to inventory)"`
        — honest about both unknowns: we don't know the canonical and the
        repo hasn't told us.

    The phrasing deliberately avoids implying "this is where Terraform
    resolves it from" (Terraform would in fact fail to init a bare
    `provider "cloudflare" {}` because the implicit fallback rule looks
    for `hashicorp/cloudflare`, which doesn't exist). The "(not declared)"
    marker is a fix-it signal, not a fallback claim."""
    canonical = _KNOWN_PROVIDER_SOURCES.get(name)
    if canonical:
        return f"{canonical} (not declared)"
    return "(not declared — unknown to inventory)"


# ─── SHA computation ─────────────────────────────────────────────────────


def compute_sha(payload: object) -> str:
    """Canonical-JSON SHA-256 (first 8 hex chars).

    Generic primitive — use the higher-level `compute_inventory_sha` /
    `compute_overview_sha` for banner-SHAs on pages. The 8-char prefix is
    enough to detect any realistic content change while keeping the banner
    readable. Pydantic models are dumped to their canonical JSON;
    lists/dicts are passed through directly.
    """
    if hasattr(payload, "model_dump"):
        data: Any = payload.model_dump(mode="json")
    elif isinstance(payload, list):
        data = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in payload]
    else:
        data = payload
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def _inventory_input_payload(inv: RepoInventory, *, model_id: str, system_prompt_version: str) -> dict[str, object]:
    """Change-detection payload for one repo's child page.

    Strictly the *inputs* that should trigger a republish — never the LLM
    output. Including `inv.narrative` here would mean narrative drift
    alone invalidates the SHA on every run, because LLM backends aren't
    reliably deterministic even at temperature=0 (different replicas /
    floating-point order / tied-token tiebreaks → slightly different
    wording for byte-identical prompts). The model id and prompt version
    are part of the payload so a backend/model swap or a manual prompt
    version bump still force-republishes the world.
    """
    return {
        "meta": inv.meta.model_dump(mode="json"),
        "summary": inv.summary.model_dump(mode="json"),
        "model_id": model_id,
        "system_prompt_version": system_prompt_version,
    }


def compute_inventory_sha(inv: RepoInventory, *, model_id: str, system_prompt_version: str) -> str:
    """Banner-SHA for a child page. See `_inventory_input_payload`."""
    return compute_sha(_inventory_input_payload(inv, model_id=model_id, system_prompt_version=system_prompt_version))


def compute_overview_sha(inventories: list[RepoInventory], *, model_id: str, system_prompt_version: str) -> str:
    """Banner-SHA for the overview page. Hashes the list of per-repo input
    payloads — same exclusion rules as `compute_inventory_sha`."""
    payloads = [
        _inventory_input_payload(inv, model_id=model_id, system_prompt_version=system_prompt_version)
        for inv in inventories
    ]
    return compute_sha(payloads)


# ─── Banner injection + extraction ──────────────────────────────────────


def build_banner(
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None = None,
) -> dict[str, Any]:
    """ADF info-panel block with the auto-generated banner."""
    timestamp = updated_at.astimezone(UTC).isoformat(timespec="seconds")
    paragraphs: list[dict[str, Any]] = [
        _paragraph(
            [
                _text(f"{BANNER_LEAD} — do not edit. ", marks=[{"type": "strong"}]),
                _text("Manual edits will be overwritten on the next scheduled run."),
            ]
        ),
        _paragraph(
            [
                _text("Last updated: ", marks=[{"type": "strong"}]),
                _text(timestamp),
            ]
        ),
        _paragraph(
            [
                _text(f"{BANNER_SHA_LABEL} ", marks=[{"type": "strong"}]),
                _text(sha, marks=[{"type": "code"}]),
            ]
        ),
    ]
    if pipeline_url:
        paragraphs.append(
            _paragraph(
                [
                    _text("Pipeline: ", marks=[{"type": "strong"}]),
                    _text(
                        pipeline_url,
                        marks=[{"type": "link", "attrs": {"href": pipeline_url}}],
                    ),
                ]
            )
        )
    return {"type": "panel", "attrs": {"panelType": "info"}, "content": paragraphs}


def extract_banner_sha(adf: dict[str, Any] | None) -> str | None:
    """Best-effort: scan an existing ADF page for the banner's `Source SHA:` line.

    Returns the SHA string or `None` if the banner is missing / malformed.
    Never raises — bad data simply means "treat the page as changed."
    """
    if not isinstance(adf, dict):
        return None
    queue: list[Any] = [adf]
    while queue:
        node = queue.pop()
        if not isinstance(node, dict):
            continue
        # Walk children
        children = node.get("content")
        if isinstance(children, list):
            queue.extend(children)
        # Look for the SHA-bearing paragraph
        if node.get("type") == "paragraph":
            text_runs = node.get("content") or []
            joined = "".join(_text_of(r) for r in text_runs if isinstance(r, dict))
            if BANNER_SHA_LABEL in joined:
                # Take the substring after the label, strip whitespace, return the
                # first whitespace-bounded token.
                tail = joined.split(BANNER_SHA_LABEL, 1)[1].strip()
                token = tail.split()[0] if tail else ""
                return token or None
    return None


def _text_of(run: dict[str, Any]) -> str:
    if run.get("type") == "text":
        t = run.get("text")
        if isinstance(t, str):
            return t
    return ""


# ─── Page builders ──────────────────────────────────────────────────────


def build_overview(
    inventories: list[RepoInventory],
    child_page_ids: dict[str, str],
    *,
    sha: str,
    updated_at: datetime,
    space_key: str,
    pipeline_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the overview page (1 per inventory).

    `child_page_ids` maps repo `full_name` → Confluence page ID; passed in
    by the orchestrator after each child page is upserted. If a name isn't
    in the map yet (first run, child created after the overview link), we
    render the repo name as plain text.

    `space_key` is the Confluence space the pages live in (e.g. "DevOps");
    used to build the canonical `/wiki/spaces/{space_key}/pages/{id}` URL
    for each row's repository link.
    """
    banner = build_banner(sha, updated_at, pipeline_url)
    intro_blocks = _build_overview_intro()
    table = _build_overview_table(inventories, child_page_ids, space_key)
    summary_block = _build_overview_summary(inventories)
    doc: dict[str, Any] = {
        "type": "doc",
        "version": 1,
        "content": [
            banner,
            *intro_blocks,
            _heading(2, "Inventory"),
            _paragraph(
                [
                    _text(
                        f"This page catalogs {len(inventories)} Terraform/IaC repositor"
                        f"{'y' if len(inventories) == 1 else 'ies'} discovered across "
                        "the configured GitLab groups and GitHub organisations. Each row "
                        "links to a child page with the full deep dive."
                    )
                ]
            ),
            table,
            _heading(2, "At a glance"),
            summary_block,
        ],
    }
    return OVERVIEW_TITLE, doc


# Public-facing source URL for the "About this page" block. Override by
# forking + replacing this constant if you self-host the tool's source code.
_IAC_CARTOGRAPHER_SOURCE_URL = "https://github.com/vakaobr/iac-cartographer"


def _build_overview_intro() -> list[dict[str, Any]]:
    """Static "About this page" block that sits between the banner and the
    inventory table. Explains who/what/why so a reader landing here from a
    deep link knows what they're looking at and where the tool lives."""
    return [
        _heading(2, "About this page"),
        _paragraph(
            [
                _text(
                    "This page is a living inventory of every Terraform / IaC repository "
                    "this organisation operates. It is regenerated on a scheduled cadence "
                    "by an automated pipeline so engineers and stakeholders always have an "
                    "up-to-date map of the infrastructure surface area — which providers "
                    "are in use, which modules are shared, and what each repository "
                    "actually does."
                )
            ]
        ),
        _paragraph(
            [
                _text(
                    "The pipeline discovers repositories across the configured VCS hosts, "
                    "extracts structural facts with "
                ),
                _link_marker("terraform-docs", "https://terraform-docs.io"),
                _text(
                    ", and asks an LLM to write the short purpose summary for each "
                    "repository. Pages publish to this Confluence space directly from "
                    "the pipeline; manual edits will be overwritten on the next run."
                ),
            ]
        ),
        _paragraph(
            [
                _text("Pipeline source: "),
                _link_marker("iac-cartographer", _IAC_CARTOGRAPHER_SOURCE_URL),
                _text(
                    " (open source). To change how a page renders, open a PR upstream or "
                    "fork the project. To change WHICH repositories are scanned, edit the "
                    "discovery config in your deployment (typically an SSM parameter or "
                    "a YAML file)."
                ),
            ]
        ),
    ]


def build_child(
    inv: RepoInventory,
    *,
    sha: str,
    updated_at: datetime,
    pipeline_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build one child page for a single `RepoInventory`."""
    banner = build_banner(sha, updated_at, pipeline_url)
    content: list[dict[str, Any]] = [
        banner,
        _heading(2, inv.meta.full_name),
        _paragraph(
            [
                _text("Repository: "),
                _text(
                    inv.meta.web_url,
                    marks=[{"type": "link", "attrs": {"href": inv.meta.web_url}}],
                ),
                _text(f" · Default branch: {inv.meta.default_branch}"),
                _text(f" · Last commit: {inv.meta.last_commit_at.strftime('%Y-%m-%d')}"),
                _text(f" ({inv.meta.last_commit_sha[:8]})"),
                # AI-H3: surface the author so a reader can trace back from a
                # suspicious narrative to whoever last touched the source.
                _text(f" by {inv.meta.last_commit_author}" if inv.meta.last_commit_author else ""),
            ]
        ),
    ]

    if inv.narrative is not None:
        content.append(_heading(3, "Purpose"))
        content.append(_paragraph([_text(inv.narrative.purpose)]))
        if inv.narrative.environments:
            content.append(_heading(3, "Environments"))
            content.append(_paragraph([_text(", ".join(inv.narrative.environments))]))
        if inv.narrative.owning_team_guess:
            content.append(_heading(3, "Owning team (guess)"))
            content.append(_paragraph([_text(inv.narrative.owning_team_guess)]))
        if inv.narrative.notable_patterns:
            content.append(_heading(3, "Notable patterns"))
            content.append(_bullet_list(inv.narrative.notable_patterns))
        if inv.narrative.key_resources_explained:
            content.append(_heading(3, "Key resources"))
            content.append(
                _table(
                    headers=["Resource type", "Why it exists"],
                    rows=[[e.resource_type, e.why_it_exists] for e in inv.narrative.key_resources_explained],
                )
            )
    else:
        content.append(_heading(3, "Purpose"))
        content.append(
            _paragraph(
                [
                    _text(
                        "(Narrative summary unavailable for this run — the LLM "
                        "invocation failed. Structural facts below are unaffected.)",
                        marks=[{"type": "em"}],
                    )
                ]
            )
        )

    # Structural sections
    s = inv.summary
    if s.module_paths:
        # "Module layout" — the directory tree the extractor actually ran
        # terraform-docs against. Surfaces multi-env layouts (e.g.
        # op-infrastructure's `terraform/env/{dev,staging,prod}/`) at a
        # glance so a Confluence reader sees the shape of the repo without
        # cloning it. Bullet list rather than table because the rendered
        # column would be just the path; a table would be visual noise.
        content.append(_heading(3, "Module layout"))
        content.append(_bullet_list(s.module_paths))
    if s.providers:
        content.append(_heading(3, "Providers"))
        # Empty source/version on a ProviderRef means terraform-docs didn't
        # find a matching `terraform { required_providers { ... } }` entry —
        # the repo wrote the bare `provider "x" {}` form. We surface the
        # canonical source via `infer_provider_source()` (curated map +
        # `hashicorp/<name>` fallback) and mark unpinned versions explicitly
        # so the cell never reads as "data missing".
        content.append(
            _table(
                headers=["Name", "Source", "Version", "Alias"],
                rows=[
                    [
                        p.name,
                        p.source or infer_provider_source(p.name),
                        p.version or "(unpinned)",
                        p.alias or "—",
                    ]
                    for p in s.providers
                ],
            )
        )
    if s.modules:
        content.append(_heading(3, "Modules"))
        content.append(
            _table(
                headers=["Name", "Source", "Version"],
                rows=[[m.name, m.source, m.version or ""] for m in s.modules],
            )
        )
    if s.resource_counts_by_type:
        content.append(_heading(3, "Resources by type"))
        content.append(
            _table(
                headers=["Type", "Count"],
                rows=[
                    [t, str(c)] for t, c in sorted(s.resource_counts_by_type.items(), key=lambda kv: (-kv[1], kv[0]))
                ],
            )
        )
    if s.inputs:
        content.append(_heading(3, "Inputs"))
        content.append(
            _table(
                headers=["Name", "Type", "Required", "Description"],
                rows=[
                    [
                        v.name,
                        v.type or "",
                        "yes" if v.required else "no",
                        (v.description or "")[:120],
                    ]
                    for v in s.inputs
                ],
            )
        )
    if s.outputs:
        content.append(_heading(3, "Outputs"))
        content.append(
            _table(
                headers=["Name", "Description"],
                rows=[[o.name, (o.description or "")[:200]] for o in s.outputs],
            )
        )

    return inv.meta.full_name, {"type": "doc", "version": 1, "content": content}


def _build_overview_table(
    inventories: list[RepoInventory], child_page_ids: dict[str, str], space_key: str
) -> dict[str, Any]:
    rows: list[list[Any]] = []
    for inv in sorted(inventories, key=lambda i: i.meta.full_name):
        meta = inv.meta
        purpose = (inv.narrative.purpose if inv.narrative else "(no narrative)")[:120]
        providers = ", ".join(p.name for p in inv.summary.providers) or "—"
        envs = ", ".join(inv.narrative.environments) if inv.narrative and inv.narrative.environments else "—"
        resource_count = sum(inv.summary.resource_counts_by_type.values())
        link_target = child_page_ids.get(meta.full_name)
        repo_cell = _repo_cell(meta.full_name, link_target, space_key)
        rows.append(
            [
                repo_cell,
                meta.host,
                providers,
                envs,
                f"{resource_count} resources",
                meta.last_commit_at.strftime("%Y-%m-%d"),
                purpose,
            ]
        )
    return _table(
        headers=[
            "Repository",
            "Host",
            "Providers",
            "Environments",
            "Resources",
            "Last commit",
            "Purpose",
        ],
        rows=rows,
    )


def _repo_cell(full_name: str, page_id: str | None, space_key: str) -> Any:
    """Return a Repository-column cell value.

    If `page_id` is known, we emit an ADF text node with a Confluence-internal
    link via `attrs.href = "/wiki/spaces/{space_key}/pages/{id}"` — the
    canonical Atlassian Cloud URL format. The short `/wiki/pages/{id}` form
    relied on a 302-redirect that doesn't fire for all space permissions.
    Otherwise we emit plain text — the next run will fill in the link once
    the child page exists.
    """
    if page_id:
        return _link_marker(full_name, f"/wiki/spaces/{space_key}/pages/{page_id}")
    return full_name


def _build_overview_summary(inventories: list[RepoInventory]) -> dict[str, Any]:
    """Bullet list of cross-cutting facts: # repos, total resources, top
    providers, common environments."""
    total = len(inventories)
    total_resources = sum(sum(inv.summary.resource_counts_by_type.values()) for inv in inventories)
    provider_counter: dict[str, int] = {}
    for inv in inventories:
        for p in inv.summary.providers:
            provider_counter[p.name] = provider_counter.get(p.name, 0) + 1
    top_providers = sorted(provider_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

    bullets = [
        f"{total} repositories indexed",
        f"{total_resources} Terraform-managed resources across all repos",
    ]
    if top_providers:
        readable = ", ".join(f"{name} ({count} repos)" for name, count in top_providers)
        bullets.append(f"Top providers: {readable}")
    return _bullet_list(bullets)


# ─── ADF primitives ─────────────────────────────────────────────────────


def _text(value: str, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        out["marks"] = marks
    return out


def _link_marker(visible: str, href: str) -> dict[str, Any]:
    return _text(visible, marks=[{"type": "link", "attrs": {"href": href}}])


def _paragraph(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "paragraph", "content": content}


def _heading(level: int, text: str) -> dict[str, Any]:
    return {"type": "heading", "attrs": {"level": level}, "content": [_text(text)]}


def _bullet_list(items: list[str]) -> dict[str, Any]:
    return {
        "type": "bulletList",
        "content": [
            {
                "type": "listItem",
                "content": [_paragraph([_text(item)])],
            }
            for item in items
        ],
    }


def _table(headers: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    header_row = {
        "type": "tableRow",
        "content": [{"type": "tableHeader", "content": [_paragraph([_text(h)])]} for h in headers],
    }
    body_rows = [
        {
            "type": "tableRow",
            "content": [{"type": "tableCell", "content": [_paragraph([_cell_run(c)])]} for c in row],
        }
        for row in rows
    ]
    return {"type": "table", "content": [header_row, *body_rows]}


def _cell_run(value: Any) -> dict[str, Any]:
    """Cells accept either a plain string or a pre-built text-node dict
    (used for repo-cell links). We pass dict values through verbatim."""
    if isinstance(value, dict):
        return value
    return _text(str(value))


__all__ = [
    "BANNER_LEAD",
    "BANNER_SHA_LABEL",
    "OVERVIEW_TITLE",
    "build_banner",
    "build_child",
    "build_overview",
    "compute_inventory_sha",
    "compute_overview_sha",
    "compute_sha",
    "extract_banner_sha",
]
