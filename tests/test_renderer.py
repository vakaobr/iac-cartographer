"""Phase 6 tests for iac_cartographer.renderer — pure-function ADF assembly."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from iac_cartographer.models import (
    BedrockNarrative,
    ProviderRef,
    RepoInventory,
    RepoMetadata,
    ResourceExplanation,
    ResourceRef,
    TerraformSummary,
    VariableRef,
)
from iac_cartographer.renderer import (
    BANNER_LEAD,
    BANNER_SHA_LABEL,
    OVERVIEW_TITLE,
    build_banner,
    build_child,
    build_overview,
    compute_inventory_sha,
    compute_overview_sha,
    compute_sha,
    extract_banner_sha,
    infer_provider_source,
)


def _meta(name: str = "acme/iac/main-cluster", host: str = "gitlab") -> RepoMetadata:
    return RepoMetadata(
        host=host,  # type: ignore[arg-type]
        full_name=name,
        clone_url=f"https://x.test/{name}.git",
        web_url=f"https://x.test/{name}",
        default_branch="main",
        last_commit_sha="a" * 40,
        last_commit_at=datetime(2026, 5, 22, tzinfo=UTC),
    )


def _inventory(name: str = "acme/iac/main-cluster", with_narrative: bool = True) -> RepoInventory:
    summary = TerraformSummary(
        providers=[ProviderRef(name="aws", source="hashicorp/aws", version=">= 6.0")],
        resources=[
            ResourceRef(type="aws_iam_role", name="task"),
            ResourceRef(type="grafana_dashboard", name="overview"),
        ],
        inputs=[VariableRef(name="region", type="string", required=False)],
        resource_counts_by_type={"aws_iam_role": 1, "grafana_dashboard": 1},
    )
    narrative = (
        BedrockNarrative(
            purpose="Provisions Grafana dashboards and IAM roles for observability.",
            key_resources_explained=[
                ResourceExplanation(resource_type="grafana_dashboard", why_it_exists="UI overview")
            ],
            environments=["prod"],
            owning_team_guess="Platform",
            notable_patterns=["one dashboard per service"],
        )
        if with_narrative
        else None
    )
    return RepoInventory(meta=_meta(name), summary=summary, narrative=narrative)


# ─── compute_sha ─────────────────────────────────────────────────────────


_SHA_KWARGS = {"model_id": "eu.anthropic.claude-sonnet-4-6", "system_prompt_version": "v1"}


def test_compute_sha_is_deterministic() -> None:
    inv = _inventory()
    assert compute_sha(inv) == compute_sha(inv)


def test_compute_sha_on_plain_list_of_inventories() -> None:
    invs = [_inventory("op/a"), _inventory("op/b")]
    sha = compute_sha(invs)
    assert len(sha) == 8
    assert all(c in "0123456789abcdef" for c in sha)


def test_compute_sha_on_plain_dict() -> None:
    sha = compute_sha({"a": 1, "b": 2})
    assert len(sha) == 8


# ─── compute_inventory_sha (banner-SHA for child pages) ──────────────────


def test_compute_inventory_sha_is_deterministic() -> None:
    inv = _inventory()
    assert compute_inventory_sha(inv, **_SHA_KWARGS) == compute_inventory_sha(inv, **_SHA_KWARGS)


def test_compute_inventory_sha_ignores_narrative_drift() -> None:
    """LLM output noise must NOT invalidate the SHA — backends aren't
    reliably deterministic even at temperature=0, so including narrative
    would force a republish every run."""
    with_narrative = _inventory(with_narrative=True)
    without_narrative = RepoInventory(meta=with_narrative.meta, summary=with_narrative.summary, narrative=None)
    different_narrative = RepoInventory(
        meta=with_narrative.meta,
        summary=with_narrative.summary,
        narrative=BedrockNarrative(
            purpose="A different purpose statement of sufficient length to pass validation.",
            key_resources_explained=[],
            environments=["staging"],
            owning_team_guess="Some Other Team",
            notable_patterns=["different pattern"],
        ),
    )
    sha = compute_inventory_sha(with_narrative, **_SHA_KWARGS)
    assert compute_inventory_sha(without_narrative, **_SHA_KWARGS) == sha
    assert compute_inventory_sha(different_narrative, **_SHA_KWARGS) == sha


def test_compute_inventory_sha_changes_on_summary_change() -> None:
    a = _inventory()
    b_summary = a.summary.model_copy(
        update={"providers": [ProviderRef(name="aws", source="hashicorp/aws", version=">= 7.0")]}
    )
    b = RepoInventory(meta=a.meta, summary=b_summary, narrative=a.narrative)
    assert compute_inventory_sha(a, **_SHA_KWARGS) != compute_inventory_sha(b, **_SHA_KWARGS)


def test_compute_inventory_sha_changes_on_meta_change() -> None:
    a = _inventory()
    b_meta = a.meta.model_copy(update={"last_commit_sha": "b" * 40})
    b = RepoInventory(meta=b_meta, summary=a.summary, narrative=a.narrative)
    assert compute_inventory_sha(a, **_SHA_KWARGS) != compute_inventory_sha(b, **_SHA_KWARGS)


def test_compute_inventory_sha_changes_on_model_swap() -> None:
    """A model swap must force-republish — narratives shift in tone even if
    the structured fields parse identically."""
    inv = _inventory()
    a = compute_inventory_sha(inv, model_id="eu.anthropic.claude-sonnet-4-6", system_prompt_version="v1")
    b = compute_inventory_sha(inv, model_id="eu.anthropic.claude-sonnet-4-5-20250929-v1:0", system_prompt_version="v1")
    assert a != b


def test_compute_inventory_sha_changes_on_prompt_version_bump() -> None:
    """The `system_prompt_version` knob is the manual force-republish lever."""
    inv = _inventory()
    a = compute_inventory_sha(inv, model_id="eu.anthropic.claude-sonnet-4-6", system_prompt_version="v1")
    b = compute_inventory_sha(inv, model_id="eu.anthropic.claude-sonnet-4-6", system_prompt_version="v2")
    assert a != b


# ─── compute_overview_sha ─────────────────────────────────────────────────


def test_compute_overview_sha_ignores_narrative_drift() -> None:
    a = [_inventory("acme/a"), _inventory("acme/b")]
    b = [RepoInventory(meta=inv.meta, summary=inv.summary, narrative=None) for inv in a]
    assert compute_overview_sha(a, **_SHA_KWARGS) == compute_overview_sha(b, **_SHA_KWARGS)


def test_compute_overview_sha_changes_on_repo_added() -> None:
    a = [_inventory("acme/a")]
    b = [_inventory("acme/a"), _inventory("acme/b")]
    assert compute_overview_sha(a, **_SHA_KWARGS) != compute_overview_sha(b, **_SHA_KWARGS)


# ─── State backend (#94) ──────────────────────────────────────────────


def _inventory_with_backend(*, encrypt: bool = True, type: str = "s3") -> RepoInventory:  # noqa: A002 — argument name mirrors HCL keyword
    from iac_cartographer.models import StateBackend, StateBackendSignal

    inv = _inventory()
    signals = (
        [StateBackendSignal(label="Encryption", value="enabled", severity="ok")]
        if encrypt
        else [StateBackendSignal(label="Encryption", value="not declared", severity="warn")]
    )
    backend = StateBackend(
        module_path=".",
        type=type,
        attrs={"bucket": '"x"', "key": '"prod/main.tfstate"', "region": '"eu-central-1"'},
        signals=signals,
    )
    return RepoInventory(
        meta=inv.meta,
        summary=inv.summary.model_copy(update={"state_backends": [backend]}),
        narrative=inv.narrative,
    )


def test_child_page_renders_state_backend_section_when_present() -> None:
    inv = _inventory_with_backend()
    _, doc = build_child(inv, sha="abcdef12", updated_at=datetime(2026, 6, 2, tzinfo=UTC))
    headings = [b for b in doc["content"] if b.get("type") == "heading"]
    heading_texts = [h["content"][0]["text"] for h in headings]
    assert "State backend" in heading_texts


def test_child_page_omits_state_backend_section_when_empty() -> None:
    inv = _inventory()  # default _inventory has no state_backends
    _, doc = build_child(inv, sha="abcdef12", updated_at=datetime(2026, 6, 2, tzinfo=UTC))
    headings = [b for b in doc["content"] if b.get("type") == "heading"]
    assert "State backend" not in [h["content"][0]["text"] for h in headings]


def test_compute_inventory_sha_changes_when_state_backend_changes() -> None:
    """A backend swap (or any change inside the StateBackend payload) MUST
    invalidate the banner-SHA — the page needs to republish."""
    encrypted = _inventory_with_backend(encrypt=True)
    unencrypted = _inventory_with_backend(encrypt=False)
    assert compute_inventory_sha(encrypted, **_SHA_KWARGS) != compute_inventory_sha(unencrypted, **_SHA_KWARGS)


def test_compute_inventory_sha_changes_when_backend_type_changes() -> None:
    s3 = _inventory_with_backend(type="s3")
    local_b = _inventory_with_backend(type="local")
    assert compute_inventory_sha(s3, **_SHA_KWARGS) != compute_inventory_sha(local_b, **_SHA_KWARGS)


def test_compute_inventory_sha_changes_when_max_nodes_per_graph_changes() -> None:
    """Threshold change triggers re-render (different chunk count → different
    rendered page), so the SHA must invalidate."""
    inv = _inventory()
    sha_default = compute_inventory_sha(inv, **_SHA_KWARGS, max_nodes_per_graph=25)
    sha_overridden = compute_inventory_sha(inv, **_SHA_KWARGS, max_nodes_per_graph=5)
    assert sha_default != sha_overridden


def test_child_page_emits_mermaid_block_when_resources_present() -> None:
    """Smoke check that the ADF child page actually contains a `codeBlock`
    with `language: "mermaid"` when the inventory has resources."""
    inv = _inventory()  # the fixture has 2 resources
    _, doc = build_child(inv, sha="abcdef12", updated_at=datetime(2026, 6, 2, tzinfo=UTC))
    code_blocks = [b for b in doc["content"] if b.get("type") == "codeBlock"]
    assert any(b.get("attrs", {}).get("language") == "mermaid" for b in code_blocks)


def test_child_page_omits_mermaid_block_when_no_resources() -> None:
    """No resources → no graph → no `codeBlock` of language `mermaid`."""
    from iac_cartographer.models import TerraformSummary

    empty = RepoInventory(
        meta=_inventory().meta,
        summary=TerraformSummary(),
        narrative=_inventory().narrative,
    )
    _, doc = build_child(empty, sha="abcdef12", updated_at=datetime(2026, 6, 2, tzinfo=UTC))
    code_blocks = [b for b in doc["content"] if b.get("type") == "codeBlock"]
    assert not any(b.get("attrs", {}).get("language") == "mermaid" for b in code_blocks)


# ─── build_banner + extract_banner_sha ──────────────────────────────────


def test_banner_contains_required_fields() -> None:
    banner = build_banner("a1b2c3d4", datetime(2026, 5, 22, 5, 0, 14, tzinfo=UTC), "https://ci.test/job/1")
    assert banner["type"] == "panel"
    assert banner["attrs"]["panelType"] == "info"
    # First paragraph must lead with `AUTO-GENERATED`
    first_text = banner["content"][0]["content"][0]["text"]
    assert first_text.startswith(BANNER_LEAD)
    # Third paragraph has the SHA in a `code` mark
    sha_para = banner["content"][2]["content"]
    label = sha_para[0]["text"]
    sha_value = sha_para[1]["text"]
    assert label.startswith(BANNER_SHA_LABEL)
    assert sha_value == "a1b2c3d4"
    # Pipeline URL appears as a link
    pipeline_para = banner["content"][3]["content"]
    assert pipeline_para[1]["marks"][0]["type"] == "link"
    assert pipeline_para[1]["marks"][0]["attrs"]["href"] == "https://ci.test/job/1"


def test_banner_omits_pipeline_when_url_missing() -> None:
    banner = build_banner("abc", datetime(2026, 5, 22, tzinfo=UTC), None)
    assert len(banner["content"]) == 3  # no pipeline paragraph


def test_extract_banner_sha_round_trips() -> None:
    sha = "a1b2c3d4"
    banner = build_banner(sha, datetime(2026, 5, 22, tzinfo=UTC), None)
    doc = {"type": "doc", "version": 1, "content": [banner]}
    assert extract_banner_sha(doc) == sha


def test_extract_banner_sha_missing_returns_none() -> None:
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]}
    assert extract_banner_sha(doc) is None


def test_extract_banner_sha_handles_none_input() -> None:
    assert extract_banner_sha(None) is None


def test_extract_banner_sha_handles_garbage_input() -> None:
    assert extract_banner_sha("not a dict") is None  # type: ignore[arg-type]
    assert extract_banner_sha({"random": "shape"}) is None


# ─── build_overview ────────────────────────────────────────────────────


def test_build_overview_title_and_doc_shape() -> None:
    title, doc = build_overview(
        [_inventory()],
        child_page_ids={},
        sha="abcd1234",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        space_key="ENG",
    )
    assert title == OVERVIEW_TITLE
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    # First block is the banner; check it's an info panel
    assert doc["content"][0]["type"] == "panel"


def test_build_overview_includes_about_this_page_intro() -> None:
    """The overview page MUST carry a static "About this page" introduction
    so a reader landing on it (e.g. from a Confluence search) understands
    what the page is and where the code that produces it lives. Added
    2026-05-25 after first production roll-out."""
    _, doc = build_overview(
        [_inventory()],
        child_page_ids={},
        sha="x",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        space_key="ENG",
    )
    headings = [b for b in doc["content"] if b.get("type") == "heading"]
    heading_texts = [h["content"][0]["text"] for h in headings]
    assert "About this page" in heading_texts
    # The source-code link must point at the iac-cartographer module on gitlab.example.com.
    all_text = json.dumps(doc)
    assert "github.com/vakaobr/iac-cartographer" in all_text


def test_build_overview_table_has_expected_columns() -> None:
    _, doc = build_overview(
        [_inventory()],
        child_page_ids={},
        sha="abcd1234",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        space_key="ENG",
    )
    # Find the first table in the doc
    tables = [b for b in doc["content"] if b.get("type") == "table"]
    assert len(tables) >= 1
    header_row = tables[0]["content"][0]
    header_cells = [
        c["content"][0]["content"][0]["text"] for c in header_row["content"] if c.get("type") == "tableHeader"
    ]
    assert header_cells == [
        "Repository",
        "Host",
        "Providers",
        "Environments",
        "Resources",
        "Last commit",
        "Purpose",
    ]


def test_build_overview_links_to_child_id_when_provided() -> None:
    inv = _inventory()
    _, doc = build_overview(
        [inv],
        child_page_ids={inv.meta.full_name: "PAGE-123"},
        sha="x",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        space_key="ENG",
    )
    tables = [b for b in doc["content"] if b.get("type") == "table"]
    first_data_row = tables[0]["content"][1]
    repo_cell_text = first_data_row["content"][0]["content"][0]["content"][0]
    assert repo_cell_text["marks"][0]["type"] == "link"
    assert "PAGE-123" in repo_cell_text["marks"][0]["attrs"]["href"]


def test_build_overview_link_uses_canonical_space_url() -> None:
    """Regression test: the repo-cell href must be the canonical
    `/wiki/spaces/{space_key}/pages/{id}` form, not the short
    `/wiki/pages/{id}` that doesn't 302-redirect in every permission setup."""
    inv = _inventory()
    _, doc = build_overview(
        [inv],
        child_page_ids={inv.meta.full_name: "PAGE-123"},
        sha="x",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        space_key="ENG",
    )
    tables = [b for b in doc["content"] if b.get("type") == "table"]
    first_data_row = tables[0]["content"][1]
    repo_cell_text = first_data_row["content"][0]["content"][0]["content"][0]
    href = repo_cell_text["marks"][0]["attrs"]["href"]
    assert href == "/wiki/spaces/ENG/pages/PAGE-123"
    assert "/spaces/" in href  # the load-bearing fragment


def test_build_overview_no_link_when_child_id_unknown() -> None:
    inv = _inventory()
    _, doc = build_overview(
        [inv],
        child_page_ids={},
        sha="x",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
        space_key="ENG",
    )
    tables = [b for b in doc["content"] if b.get("type") == "table"]
    first_data_row = tables[0]["content"][1]
    repo_cell_text = first_data_row["content"][0]["content"][0]["content"][0]
    assert "marks" not in repo_cell_text or not repo_cell_text["marks"]


def test_build_overview_deterministic() -> None:
    invs = [_inventory("op/a"), _inventory("op/b")]
    a = build_overview(invs, {}, sha="x", updated_at=datetime(2026, 5, 22, tzinfo=UTC), space_key="ENG")
    b = build_overview(invs, {}, sha="x", updated_at=datetime(2026, 5, 22, tzinfo=UTC), space_key="ENG")
    assert a == b


# ─── build_child ────────────────────────────────────────────────────────


def test_build_child_has_purpose_and_tables() -> None:
    title, doc = build_child(_inventory(), sha="x", updated_at=datetime(2026, 5, 22, tzinfo=UTC))
    assert title == "acme/iac/main-cluster"
    # Find headings — should include Purpose, Environments, Owning team, Notable patterns,
    # Key resources, Providers, Resources by type, Inputs.
    headings = [b for b in doc["content"] if b.get("type") == "heading"]
    heading_texts = [h["content"][0]["text"] for h in headings]
    assert "Purpose" in heading_texts
    assert "Environments" in heading_texts
    assert "Notable patterns" in heading_texts
    assert "Providers" in heading_texts
    assert "Resources by type" in heading_texts
    assert "Inputs" in heading_texts


def test_build_child_handles_missing_narrative() -> None:
    title, doc = build_child(
        _inventory(with_narrative=False),
        sha="x",
        updated_at=datetime(2026, 5, 22, tzinfo=UTC),
    )
    assert title.endswith("main-cluster")
    # Purpose section exists but says narrative-unavailable
    purpose_paragraphs = [p for p in doc["content"] if p.get("type") == "paragraph"]
    joined = " ".join(run.get("text", "") for p in purpose_paragraphs for run in p.get("content", []))
    assert "Narrative summary unavailable" in joined


# ─── infer_provider_source + Providers table integration ───────────────


def test_infer_provider_source_known_vendor() -> None:
    """Curated vendor providers carry their canonical registry path plus a
    `(not declared)` tag — the marker communicates "the repo is missing
    its required_providers entry", which matters because modern Terraform
    will fail to init for any non-Hashicorp namespace lacking that block."""
    assert infer_provider_source("hcloud") == "hetznercloud/hcloud (not declared)"
    assert infer_provider_source("cloudflare") == "cloudflare/cloudflare (not declared)"
    assert infer_provider_source("gitlab") == "gitlabhq/gitlab (not declared)"


def test_infer_provider_source_known_hashicorp() -> None:
    """Hashicorp's own providers are also in the map; their canonical path
    is rendered (even though Terraform's legacy implicit-fallback rule
    would resolve them correctly anyway)."""
    assert infer_provider_source("aws") == "hashicorp/aws (not declared)"
    assert infer_provider_source("random") == "hashicorp/random (not declared)"
    assert infer_provider_source("kubernetes") == "hashicorp/kubernetes (not declared)"


def test_infer_provider_source_unknown_says_so() -> None:
    """Providers not in the curated map render an explicit
    "unknown to inventory" marker — no `hashicorp/<name>` guess, since
    that's wrong for every modern vendor provider and a guess in the
    table is worse than no claim at all."""
    assert infer_provider_source("brand_new_provider") == "(not declared — unknown to inventory)"


def test_build_child_providers_table_uses_declared_source_when_present() -> None:
    """A provider WITH a declared source renders exactly as terraform-docs
    reported it — no `(not declared)` suffix, no override of the explicit
    value."""
    inv = RepoInventory(
        meta=_meta("op/foo"),
        summary=TerraformSummary(providers=[ProviderRef(name="aws", source="hashicorp/aws", version=">= 6.0")]),
        narrative=None,
    )
    _, doc = build_child(inv, sha="x", updated_at=datetime(2026, 5, 22, tzinfo=UTC))
    tables = [b for b in doc["content"] if b.get("type") == "table"]
    aws_row = tables[0]["content"][1]
    cells = [c["content"][0]["content"][0]["text"] for c in aws_row["content"]]
    assert cells == ["aws", "hashicorp/aws", ">= 6.0", "—"]


def test_build_child_providers_table_marks_blank_source_as_not_declared() -> None:
    """A `provider "cloudflare" {}` without a matching `required_providers`
    block renders as `cloudflare/cloudflare (not declared)` plus `(unpinned)`
    for the version. The "(not declared)" suffix is the fix-it signal: the
    repo should declare `source = "cloudflare/cloudflare"` and a version
    constraint in `terraform { required_providers { ... } }`. Mirrors what
    we see on op/op-infrastructure today."""
    inv = RepoInventory(
        meta=_meta("op/op-infrastructure"),
        summary=TerraformSummary(
            providers=[ProviderRef(name="cloudflare")],
        ),
        narrative=None,
    )
    _, doc = build_child(inv, sha="x", updated_at=datetime(2026, 5, 22, tzinfo=UTC))
    tables = [b for b in doc["content"] if b.get("type") == "table"]
    cf_row = tables[0]["content"][1]
    cells = [c["content"][0]["content"][0]["text"] for c in cf_row["content"]]
    assert cells == ["cloudflare", "cloudflare/cloudflare (not declared)", "(unpinned)", "—"]


def test_build_child_renders_module_layout_when_paths_present() -> None:
    """Multi-env repos show their layout as a `Module layout` bullet list
    on the child page so operators see the repo's shape without cloning."""
    inv = RepoInventory(
        meta=_meta("op/op-infrastructure"),
        summary=TerraformSummary(
            module_paths=["terraform/env/dev", "terraform/env/prod", "terraform/env/staging"],
        ),
        narrative=None,
    )
    _, doc = build_child(inv, sha="x", updated_at=datetime(2026, 5, 22, tzinfo=UTC))
    headings = [b["content"][0]["text"] for b in doc["content"] if b.get("type") == "heading"]
    assert "Module layout" in headings
    # The bullet list lives right after the heading.
    layout_idx = next(
        i
        for i, b in enumerate(doc["content"])
        if b.get("type") == "heading" and b["content"][0]["text"] == "Module layout"
    )
    bullets = doc["content"][layout_idx + 1]
    assert bullets["type"] == "bulletList"
    text_per_item = [item["content"][0]["content"][0]["text"] for item in bullets["content"]]
    assert text_per_item == ["terraform/env/dev", "terraform/env/prod", "terraform/env/staging"]


def test_build_child_omits_module_layout_when_paths_empty() -> None:
    """Single-module repos render no `Module layout` heading at all —
    the bullet would just say `.` which is visual noise."""
    inv = RepoInventory(
        meta=_meta("op/flat-repo"),
        summary=TerraformSummary(),  # no module_paths
        narrative=None,
    )
    _, doc = build_child(inv, sha="x", updated_at=datetime(2026, 5, 22, tzinfo=UTC))
    headings = [b["content"][0]["text"] for b in doc["content"] if b.get("type") == "heading"]
    assert "Module layout" not in headings


def test_build_child_omits_empty_sections() -> None:
    inv = RepoInventory(
        meta=_meta("op/empty"),
        summary=TerraformSummary(),  # all empty
        narrative=None,
    )
    _, doc = build_child(inv, sha="x", updated_at=datetime(2026, 5, 22, tzinfo=UTC))
    heading_texts = [h["content"][0]["text"] for h in doc["content"] if h.get("type") == "heading"]
    # The repo-name heading is always there; per-section ones should not be.
    assert "Providers" not in heading_texts
    assert "Resources by type" not in heading_texts
    assert "Inputs" not in heading_texts


def test_build_child_resources_by_type_sorted_by_count_desc() -> None:
    inv = _inventory()
    # Add a 3-count type so we can verify ordering
    inv.summary.resource_counts_by_type = {"aws_x": 3, "aws_iam_role": 1, "grafana_dashboard": 1}
    _, doc = build_child(inv, sha="x", updated_at=datetime(2026, 5, 22, tzinfo=UTC))
    # Find the Resources-by-type table
    headings = [b for b in doc["content"] if b.get("type") == "heading"]
    rbt_idx = next(
        i
        for i, h in enumerate(doc["content"])
        if h.get("type") == "heading" and h["content"][0]["text"] == "Resources by type"
    )
    table = doc["content"][rbt_idx + 1]
    # First data row should be the most-frequent
    first_data_row = table["content"][1]
    first_cell_text = first_data_row["content"][0]["content"][0]["content"][0]["text"]
    assert first_cell_text == "aws_x"
    assert len(headings) > 0


def test_round_trip_sha_to_extract() -> None:
    inv = _inventory()
    sha = compute_inventory_sha(inv, **_SHA_KWARGS)
    _, doc = build_child(inv, sha=sha, updated_at=datetime(2026, 5, 22, tzinfo=UTC))
    assert extract_banner_sha(doc) == sha
