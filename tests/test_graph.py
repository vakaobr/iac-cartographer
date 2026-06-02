"""Tests for `iac_cartographer.graph` — the Mermaid resource-dependency
diagram generator.

Acceptance criteria for #95:

  * Small graph happy path                    → one Mermaid `graph TD` string
  * Chunking trigger at threshold + 1         → multiple strings, whole providers
                                                kept together
  * Repo with zero resources                  → empty list (no diagram emitted)

Plus: provider grouping shape, deterministic output across runs (banner-SHA
stability), and label escaping for resource names that contain special chars.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from iac_cartographer.graph import build_mermaid
from iac_cartographer.models import (
    BedrockNarrative,
    RepoInventory,
    RepoMetadata,
    ResourceRef,
    TerraformSummary,
)


def _meta() -> RepoMetadata:
    return RepoMetadata(
        host="gitlab",
        full_name="acme/iac/x",
        clone_url="https://x.test/acme/iac/x.git",
        web_url="https://x.test/acme/iac/x",
        default_branch="main",
        last_commit_sha="a" * 40,
        last_commit_at=datetime(2026, 6, 2, tzinfo=UTC),
    )


def _inv(resources: list[ResourceRef]) -> RepoInventory:
    return RepoInventory(
        meta=_meta(),
        summary=TerraformSummary(resources=resources),
        narrative=BedrockNarrative(
            purpose="A sufficiently long purpose statement to satisfy validation.",
        ),
    )


# ─── Happy path ─────────────────────────────────────────────────────────


def test_small_graph_emits_one_chunk() -> None:
    inv = _inv(
        [
            ResourceRef(type="aws_iam_role", name="task"),
            ResourceRef(type="aws_s3_bucket", name="state"),
            ResourceRef(type="grafana_dashboard", name="overview"),
        ]
    )
    chunks = build_mermaid(inv)
    assert len(chunks) == 1
    diagram = chunks[0]
    assert diagram.startswith("graph TD\n")
    # Provider nodes use the stadium shape `(["..."])`.
    assert '(["aws"])' in diagram
    assert '(["grafana"])' in diagram
    # All three resources labelled `type.name`.
    assert "aws_iam_role.task" in diagram
    assert "aws_s3_bucket.state" in diagram
    assert "grafana_dashboard.overview" in diagram
    # Provider → resource edges present.
    assert "p0 --> r0" in diagram
    # CSS class def for the provider styling — both Confluence + GitHub render it.
    assert "classDef provider" in diagram


def test_explicit_provider_attribute_wins_over_type_prefix() -> None:
    """`provider = aws.replica` overrides the prefix heuristic on the
    `type` column, matching the rest of the renderer."""
    inv = _inv(
        [
            ResourceRef(type="aws_iam_role", name="task", provider="aws.replica"),
        ]
    )
    diagram = build_mermaid(inv)[0]
    # Only "aws" appears (alias stripped), no separate "aws.replica" node.
    assert '(["aws"])' in diagram
    assert '(["aws.replica"])' not in diagram


# ─── Chunking ───────────────────────────────────────────────────────────


def test_chunking_triggers_above_threshold() -> None:
    """One provider with `threshold + 1` resources packs into a single
    oversized chunk; two providers with `threshold` and `2` resources
    split into two chunks (whole providers kept together)."""
    threshold = 5
    inv = _inv(
        [ResourceRef(type="aws_iam_role", name=f"r{i}") for i in range(threshold)]
        + [
            ResourceRef(type="grafana_dashboard", name="dash1"),
            ResourceRef(type="grafana_dashboard", name="dash2"),
        ]
    )
    chunks = build_mermaid(inv, max_nodes_per_graph=threshold)
    # Two chunks: one for aws (5 resources, packed alone), one for grafana (2).
    assert len(chunks) == 2
    # Each chunk only mentions its own provider.
    aws_chunk, grafana_chunk = sorted(chunks, key=lambda c: '(["aws"])' not in c)
    assert '(["aws"])' in aws_chunk and '(["grafana"])' not in aws_chunk
    assert '(["grafana"])' in grafana_chunk and '(["aws"])' not in grafana_chunk


def test_below_threshold_returns_single_chunk() -> None:
    """Resource count == threshold stays in one chunk (the threshold is
    inclusive — chunking triggers above it, not at it)."""
    threshold = 3
    inv = _inv(
        [
            ResourceRef(type="aws_s3_bucket", name="a"),
            ResourceRef(type="aws_iam_role", name="b"),
            ResourceRef(type="grafana_dashboard", name="c"),
        ]
    )
    chunks = build_mermaid(inv, max_nodes_per_graph=threshold)
    assert len(chunks) == 1


def test_single_oversized_provider_ships_whole() -> None:
    """A provider whose resource count alone exceeds the threshold MUST
    ship as one chunk (splitting a logical group defeats grouping)."""
    threshold = 3
    inv = _inv([ResourceRef(type="aws_iam_role", name=f"r{i}") for i in range(10)])
    chunks = build_mermaid(inv, max_nodes_per_graph=threshold)
    assert len(chunks) == 1
    assert chunks[0].count("r") >= 10  # 10 resource node IDs at least


# ─── Zero resources ─────────────────────────────────────────────────────


def test_zero_resources_returns_empty_list() -> None:
    """No resources → empty list, and the renderer skips the section."""
    inv = _inv([])
    assert build_mermaid(inv) == []


# ─── Determinism ────────────────────────────────────────────────────────


def test_output_is_deterministic_across_runs() -> None:
    """Banner-SHA stability requires byte-identical output for the same
    input. `build_mermaid` sorts resources internally before emitting."""
    a = _inv(
        [
            ResourceRef(type="aws_iam_role", name="task"),
            ResourceRef(type="grafana_dashboard", name="overview"),
            ResourceRef(type="aws_s3_bucket", name="state"),
        ]
    )
    b = _inv(
        # Same resources, different input order.
        [
            ResourceRef(type="grafana_dashboard", name="overview"),
            ResourceRef(type="aws_s3_bucket", name="state"),
            ResourceRef(type="aws_iam_role", name="task"),
        ]
    )
    assert build_mermaid(a) == build_mermaid(b)


# ─── Label escaping ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_name",
    [
        'name"with"quotes',
        "name\\with\\backslashes",
        "name\nwith\nnewlines",
    ],
)
def test_special_chars_in_resource_name_dont_break_mermaid_syntax(bad_name: str) -> None:
    """Mermaid breaks on raw quotes / backslashes / newlines inside a
    bracketed label. The escape helper turns each into an HTML entity."""
    inv = _inv([ResourceRef(type="aws_thing", name=bad_name)])
    diagram = build_mermaid(inv)[0]
    # No literal quote characters after the opening `["` of the label.
    label_section = diagram.split('r0["', 1)[1]
    closing = label_section.split('"]', 1)[0]
    assert '"' not in closing
    assert "\\" not in closing
    assert "\n" not in closing
