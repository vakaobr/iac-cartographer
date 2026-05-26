"""Tests for the between-run inventory diff (`--diff <prev-output>` mode).

Coverage:
  * compute_diff — added / removed / changed repos, provider + module
    + resource-count deltas, narrative-only changes (NOT a structural
    change), empty-prior baseline case.
  * render_diff_markdown + render_diff_summary — Markdown layout,
    one-line Slack summary, empty-diff shape.
  * load_prior_inventories — happy path against a real JSON-publisher
    layout, missing dir → empty list, malformed file → log + skip.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from iac_cartographer.diff import (
    ChangeType,
    InventoryDiff,
    ProviderChange,
    RepoDiff,
    compute_diff,
    load_prior_inventories,
    render_diff_markdown,
    render_diff_summary,
)
from iac_cartographer.models import (
    BedrockNarrative,
    ModuleRef,
    ProviderRef,
    RepoInventory,
    RepoMetadata,
    TerraformSummary,
)


def _meta(full_name: str = "acme/main-cluster") -> RepoMetadata:
    return RepoMetadata(
        host="github",
        full_name=full_name,
        clone_url=f"https://github.com/{full_name}.git",
        web_url=f"https://github.com/{full_name}",
        default_branch="main",
        last_commit_sha="abc123",
        last_commit_at=datetime(2026, 1, 15, tzinfo=UTC),
        last_commit_author="alice",
    )


def _inv(
    full_name: str = "acme/main-cluster",
    *,
    providers: list[ProviderRef] | None = None,
    modules: list[ModuleRef] | None = None,
    resource_counts: dict[str, int] | None = None,
    narrative_purpose: str | None = None,
) -> RepoInventory:
    narrative = None
    if narrative_purpose:
        narrative = BedrockNarrative(purpose=narrative_purpose, environments=["prod"])
    return RepoInventory(
        meta=_meta(full_name),
        summary=TerraformSummary(
            providers=providers or [],
            modules=modules or [],
            resource_counts_by_type=resource_counts or {},
        ),
        narrative=narrative,
    )


# ── compute_diff: repo set transitions ───────────────────────────────


def test_compute_diff_detects_added_repos() -> None:
    prior = [_inv("acme/old")]
    current = [_inv("acme/old"), _inv("acme/new1"), _inv("acme/new2")]
    diff = compute_diff(prior, current)

    assert diff.added_repos == ["acme/new1", "acme/new2"]
    assert diff.removed_repos == []
    assert diff.unchanged_count == 1


def test_compute_diff_detects_removed_repos() -> None:
    prior = [_inv("acme/a"), _inv("acme/b")]
    current = [_inv("acme/a")]
    diff = compute_diff(prior, current)

    assert diff.removed_repos == ["acme/b"]
    assert diff.added_repos == []


def test_compute_diff_first_run_treats_everything_as_added() -> None:
    """No prior → every current repo is `added_repos`. Useful as the
    initial-baseline output."""
    current = [_inv("a"), _inv("b"), _inv("c")]
    diff = compute_diff([], current)

    assert sorted(diff.added_repos) == ["a", "b", "c"]
    assert diff.removed_repos == []
    assert diff.changed_repos == []
    assert diff.unchanged_count == 0


def test_compute_diff_empty_both_sides_is_safe() -> None:
    diff = compute_diff([], [])
    assert diff.added_repos == []
    assert diff.removed_repos == []
    assert diff.changed_repos == []
    assert diff.unchanged_count == 0


# ── compute_diff: providers ──────────────────────────────────────────


def test_provider_added_surfaces_in_repo_diff() -> None:
    prior = [_inv("r", providers=[ProviderRef(name="aws", version=">= 5.0")])]
    current = [
        _inv(
            "r",
            providers=[
                ProviderRef(name="aws", version=">= 5.0"),
                ProviderRef(name="random", version=None),
            ],
        )
    ]
    diff = compute_diff(prior, current)

    assert len(diff.changed_repos) == 1
    repo_diff = diff.changed_repos[0]
    assert len(repo_diff.provider_changes) == 1
    pc = repo_diff.provider_changes[0]
    assert pc.name == "random"
    assert pc.change == ChangeType.ADDED
    assert pc.current_version is None  # unpinned


def test_provider_removed_surfaces_in_repo_diff() -> None:
    prior = [
        _inv(
            "r",
            providers=[
                ProviderRef(name="aws", version=">= 5.0"),
                ProviderRef(name="random", version=">= 3.0"),
            ],
        )
    ]
    current = [_inv("r", providers=[ProviderRef(name="aws", version=">= 5.0")])]
    diff = compute_diff(prior, current)

    repo_diff = diff.changed_repos[0]
    pc = repo_diff.provider_changes[0]
    assert pc.name == "random"
    assert pc.change == ChangeType.REMOVED
    assert pc.prior_version == ">= 3.0"


def test_provider_version_bump_surfaces_in_repo_diff() -> None:
    prior = [_inv("r", providers=[ProviderRef(name="aws", version=">= 5.0")])]
    current = [_inv("r", providers=[ProviderRef(name="aws", version=">= 6.0")])]
    diff = compute_diff(prior, current)

    pc = diff.changed_repos[0].provider_changes[0]
    assert pc.name == "aws"
    assert pc.change == ChangeType.CHANGED
    assert pc.prior_version == ">= 5.0"
    assert pc.current_version == ">= 6.0"


def test_provider_unpinned_to_pinned_counts_as_changed() -> None:
    """Going from `version=None` to a specific constraint IS a meaningful
    change — operators usually want to know about that transition."""
    prior = [_inv("r", providers=[ProviderRef(name="aws", version=None)])]
    current = [_inv("r", providers=[ProviderRef(name="aws", version=">= 5.0")])]
    diff = compute_diff(prior, current)

    pc = diff.changed_repos[0].provider_changes[0]
    assert pc.change == ChangeType.CHANGED


# ── compute_diff: modules ────────────────────────────────────────────


def test_module_version_bump_surfaces_in_repo_diff() -> None:
    prior = [_inv("r", modules=[ModuleRef(name="vpc", source="terraform-aws-modules/vpc/aws", version="4.0.0")])]
    current = [_inv("r", modules=[ModuleRef(name="vpc", source="terraform-aws-modules/vpc/aws", version="5.0.0")])]
    diff = compute_diff(prior, current)

    mc = diff.changed_repos[0].module_changes[0]
    assert mc.name == "vpc"
    assert mc.change == ChangeType.CHANGED
    assert mc.prior_version == "4.0.0"
    assert mc.current_version == "5.0.0"


def test_module_indexed_by_name_and_source() -> None:
    """Same local name `vpc` from two different sources are two distinct
    module entries — removing one and adding the other counts as
    remove+add, not a version bump."""
    prior = [_inv("r", modules=[ModuleRef(name="vpc", source="terraform-aws-modules/vpc/aws", version="5.0")])]
    current = [_inv("r", modules=[ModuleRef(name="vpc", source="./modules/vpc", version=None)])]
    diff = compute_diff(prior, current)

    changes = diff.changed_repos[0].module_changes
    actions = sorted((c.change.value, c.source) for c in changes)
    assert actions == [
        ("added", "./modules/vpc"),
        ("removed", "terraform-aws-modules/vpc/aws"),
    ]


# ── compute_diff: resource counts ────────────────────────────────────


def test_resource_count_delta_positive() -> None:
    prior = [_inv("r", resource_counts={"aws_instance": 2})]
    current = [_inv("r", resource_counts={"aws_instance": 5})]
    diff = compute_diff(prior, current)

    assert diff.changed_repos[0].resource_count_delta == 3


def test_resource_count_delta_negative() -> None:
    prior = [_inv("r", resource_counts={"aws_instance": 10, "aws_s3_bucket": 2})]
    current = [_inv("r", resource_counts={"aws_instance": 3})]
    diff = compute_diff(prior, current)

    # 3 - (10 + 2) = -9
    assert diff.changed_repos[0].resource_count_delta == -9


def test_resource_distribution_shift_without_count_change_is_not_structural() -> None:
    """Two repos with the same TOTAL count but different per-type
    distribution have `resource_count_delta == 0`. Without provider /
    module changes alongside, this is NOT considered structural —
    operators don't want noise from internal restructurings that
    didn't change the size of the estate."""
    prior = [_inv("r", resource_counts={"aws_instance": 2, "aws_s3_bucket": 1})]
    current = [_inv("r", resource_counts={"aws_lambda_function": 3})]
    diff = compute_diff(prior, current)

    # No structural change → unchanged.
    assert diff.changed_repos == []
    assert diff.unchanged_count == 1


# ── compute_diff: narrative-only changes ─────────────────────────────


def test_narrative_only_change_does_not_count_as_structural() -> None:
    """The LLM picks slightly different words each run; treating that
    as structural would flood the diff with noise. Pure narrative
    diffs go into `unchanged_count`, not `changed_repos`."""
    prior = [
        _inv(
            "r",
            providers=[ProviderRef(name="aws", version=">= 5.0")],
            narrative_purpose="Provisions the production VPC.",
        )
    ]
    current = [
        _inv(
            "r",
            providers=[ProviderRef(name="aws", version=">= 5.0")],
            narrative_purpose="Manages the production virtual private cloud.",
        )
    ]
    diff = compute_diff(prior, current)

    assert diff.changed_repos == []
    assert diff.unchanged_count == 1


def test_narrative_changed_flag_set_when_structural_change_also_present() -> None:
    """When a repo IS structurally changed and the narrative also
    differs, the flag is set on the RepoDiff for downstream
    consumers that care."""
    prior = [
        _inv(
            "r",
            providers=[ProviderRef(name="aws", version=">= 5.0")],
            narrative_purpose="Provisions the production VPC network.",
        )
    ]
    current = [
        _inv(
            "r",
            providers=[ProviderRef(name="aws", version=">= 6.0")],  # bumped
            narrative_purpose="Manages the production virtual private cloud.",
        )
    ]
    diff = compute_diff(prior, current)

    repo_diff = diff.changed_repos[0]
    assert repo_diff.narrative_changed is True


# ── render: Markdown ─────────────────────────────────────────────────


def test_render_markdown_empty_diff() -> None:
    diff = InventoryDiff(unchanged_count=10)
    out = render_diff_markdown(diff)
    assert "No changes" in out
    assert "10 repos tracked" in out


def test_render_markdown_with_all_three_categories() -> None:
    diff = InventoryDiff(
        added_repos=["acme/new1", "acme/new2"],
        removed_repos=["acme/old"],
        changed_repos=[
            RepoDiff(
                full_name="acme/main",
                provider_changes=[
                    ProviderChange(
                        name="aws",
                        change=ChangeType.CHANGED,
                        prior_version=">= 5.0",
                        current_version=">= 6.0",
                    )
                ],
                resource_count_delta=2,
            )
        ],
        unchanged_count=37,
    )
    out = render_diff_markdown(diff)

    assert "**Added (2):** acme/new1, acme/new2" in out
    assert "**Removed (1):** acme/old" in out
    assert "**Changed (1):**" in out
    assert "acme/main:" in out
    assert "provider aws bumped (>= 5.0 → >= 6.0)" in out
    assert "+2 resources" in out
    assert "37 unchanged." in out


def test_render_markdown_provider_added_uses_unpinned_marker_when_version_missing() -> None:
    diff = InventoryDiff(
        changed_repos=[
            RepoDiff(
                full_name="r",
                provider_changes=[
                    ProviderChange(name="random", change=ChangeType.ADDED, current_version=None),
                ],
            )
        ]
    )
    out = render_diff_markdown(diff)
    assert "provider random added (unpinned)" in out


# ── render: one-line summary ─────────────────────────────────────────


def test_render_summary_empty_diff() -> None:
    diff = InventoryDiff(unchanged_count=42)
    assert render_diff_summary(diff) == "no changes; 42 unchanged"


def test_render_summary_carries_each_category() -> None:
    diff = InventoryDiff(
        added_repos=["a", "b", "c"],
        removed_repos=["d"],
        changed_repos=[RepoDiff(full_name="e", resource_count_delta=1)],
        unchanged_count=37,
    )
    assert render_diff_summary(diff) == "3 new, 1 archived, 1 changed; 37 unchanged"


def test_render_summary_omits_empty_categories() -> None:
    """No mention of '0 new' when there are no additions — the line
    stays scannable."""
    diff = InventoryDiff(
        removed_repos=["d"],
        unchanged_count=10,
    )
    assert render_diff_summary(diff) == "1 archived; 10 unchanged"


# ── load_prior_inventories ───────────────────────────────────────────


def test_load_prior_inventories_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    """First-run case: nothing on disk → empty list, no error."""
    result = load_prior_inventories(tmp_path / "does-not-exist")
    assert result == []


def test_load_prior_inventories_returns_empty_when_repos_dir_missing(tmp_path: Path) -> None:
    """Directory exists but has no `repos/` subdir — also first-run."""
    (tmp_path / "index.json").write_text("{}", encoding="utf-8")
    result = load_prior_inventories(tmp_path)
    assert result == []


def test_load_prior_inventories_reads_json_publisher_layout(tmp_path: Path) -> None:
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    inv = _inv("acme/main", providers=[ProviderRef(name="aws", version=">= 5.0")])
    # Mirror what render_child_json produces — banner block + flat
    # meta/summary/narrative fields.
    payload = {
        "iac_cartographer": {"sha": "abc", "updated_at": "2026-01-01T00:00:00Z"},
        "meta": inv.meta.model_dump(mode="json"),
        "summary": inv.summary.model_dump(mode="json"),
        "narrative": None,
    }
    (repos_dir / "acme__main.json").write_text(json.dumps(payload), encoding="utf-8")

    result = load_prior_inventories(tmp_path)
    assert len(result) == 1
    assert result[0].meta.full_name == "acme/main"
    assert len(result[0].summary.providers) == 1


def test_load_prior_inventories_skips_malformed_files(tmp_path: Path) -> None:
    """One bad file shouldn't sink the whole load — log + skip, keep
    the good ones."""
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()

    # Bad: truncated JSON.
    (repos_dir / "bad-json.json").write_text("{not valid json", encoding="utf-8")
    # Bad: valid JSON but missing required RepoMetadata fields.
    (repos_dir / "missing-fields.json").write_text(json.dumps({"meta": {}, "summary": {}}), encoding="utf-8")
    # Good: full payload.
    inv = _inv("acme/good")
    payload = {
        "iac_cartographer": {"sha": "abc"},
        "meta": inv.meta.model_dump(mode="json"),
        "summary": inv.summary.model_dump(mode="json"),
        "narrative": None,
    }
    (repos_dir / "good.json").write_text(json.dumps(payload), encoding="utf-8")

    result = load_prior_inventories(tmp_path)
    assert len(result) == 1
    assert result[0].meta.full_name == "acme/good"
