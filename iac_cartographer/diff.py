"""Between-run inventory diff — `--diff <prev-output-dir>` mode.

Given a prior run's JSON-publisher output directory and the current
run's `list[RepoInventory]`, this module computes a structured diff
and renders it for human + machine consumption:

  * **Added repos** — `full_name`s that appeared (discovered for the
    first time, or un-archived).
  * **Removed repos** — `full_name`s that disappeared (archived,
    deleted, or moved out of the configured discovery scope).
  * **Changed repos** — present in both, but with structural changes
    (providers added / removed / version-bumped, modules added /
    removed / bumped, total-resource-count delta).
  * **Unchanged count** — repos in both with no structural change.
    Useful as a denominator in summary lines.

The diff is intentionally **structural** — it ignores narrative-only
changes (the LLM picks slightly different words each run; treating
that as "changed" would flood the diff with noise). The publisher's
banner-SHA short-circuit already filters narrative-only re-runs out
of the actual republish path, so the diff's `changed_repos` and the
publisher's "updated" set tend to track each other closely.

CLI hook: `iac-cartographer --diff <path>` loads the prior output
from `<path>/repos/*.json`, computes the diff after this run's
inventory is built, and prints a Markdown summary to stdout. When
notifications are configured, the same summary rides on the
end-of-run `info` post — operators reading Slack see "3 new repos,
1 archived, AWS provider bumped to 6.5 in 2 repos" instead of just
"N repos published".

First-run shape: `<path>` doesn't exist or contains no `repos/*.json`
files → `load_prior_inventories` returns `[]`, the diff is
all-additions, and the operator sees a useful "first baseline"
summary rather than an error.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from iac_cartographer.models import RepoInventory

logger = logging.getLogger("iac_cartographer.diff")


class ChangeType(StrEnum):
    """The three diff outcomes for a single provider / module entry."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"  # same name, different version


class _Strict(BaseModel):
    """Local copy of the strict-base so this module doesn't depend on
    `iac_cartographer.models._Strict` (which is intentionally private)."""

    model_config = ConfigDict(extra="forbid")


class ProviderChange(_Strict):
    """One provider's change between two inventory snapshots."""

    name: str
    change: ChangeType
    # Versions are nullable — unpinned providers carry `None`, which is
    # a meaningful state in itself (the renderer surfaces it as `(unpinned)`).
    prior_version: str | None = None
    current_version: str | None = None


class ModuleChange(_Strict):
    """One module's change between two inventory snapshots.

    Indexed by `(name, source)` so two distinct modules with the
    same local name (rare but possible) don't collide. Renaming
    the alias in HCL surfaces as remove+add rather than a rename;
    that's intentional — detecting renames would need git history
    we don't carry here."""

    name: str
    source: str
    change: ChangeType
    prior_version: str | None = None
    current_version: str | None = None


class RepoDiff(_Strict):
    """Per-repo structural diff. Carried inside `InventoryDiff`."""

    full_name: str
    provider_changes: list[ProviderChange] = Field(default_factory=list)
    module_changes: list[ModuleChange] = Field(default_factory=list)
    # `resource_count_delta = curr_total - prior_total`. Positive when
    # the repo grew; negative when shrunk; zero when the type-distribution
    # shifted but the total stayed the same (rare).
    resource_count_delta: int = 0
    # True when the narrative purpose paragraph differs between runs.
    # NOT a trigger for inclusion in `InventoryDiff.changed_repos` — the
    # LLM-author noise would dominate the diff. Surfaced as a hint
    # alongside structural changes.
    narrative_changed: bool = False


class InventoryDiff(_Strict):
    """Full diff between two inventory snapshots.

    Operators consume this as either Markdown (renderer below) or
    JSON (`model_dump(mode="json")`). Both forms are stable enough
    for downstream tooling — bump `schema_version` only when the
    field shape changes incompatibly."""

    added_repos: list[str] = Field(default_factory=list)
    removed_repos: list[str] = Field(default_factory=list)
    changed_repos: list[RepoDiff] = Field(default_factory=list)
    # Repos present in both snapshots with no structural change. We
    # carry the count rather than the list — the list is rarely useful
    # downstream and the count anchors the summary lines ("3 changed,
    # 42 unchanged").
    unchanged_count: int = 0


# ─── compute ──────────────────────────────────────────────────────────


def compute_diff(
    prior: list[RepoInventory],
    current: list[RepoInventory],
) -> InventoryDiff:
    """Compute the structural diff between two inventory lists.

    Repos are indexed by `meta.full_name`. The function is pure —
    callers can run it on any two inventory snapshots regardless of
    where they came from (JSON publisher output, in-memory, etc.)."""
    prior_by_name = {inv.meta.full_name: inv for inv in prior}
    current_by_name = {inv.meta.full_name: inv for inv in current}

    added = sorted(set(current_by_name) - set(prior_by_name))
    removed = sorted(set(prior_by_name) - set(current_by_name))
    overlap = sorted(set(prior_by_name) & set(current_by_name))

    changed: list[RepoDiff] = []
    unchanged = 0
    for name in overlap:
        repo_diff = _diff_one_repo(prior_by_name[name], current_by_name[name])
        if _is_structural_change(repo_diff):
            changed.append(repo_diff)
        else:
            unchanged += 1

    return InventoryDiff(
        added_repos=added,
        removed_repos=removed,
        changed_repos=changed,
        unchanged_count=unchanged,
    )


def _is_structural_change(diff: RepoDiff) -> bool:
    """A diff counts as a structural change when at least one of:
    provider added/removed/bumped, module added/removed/bumped, or
    the total resource count moved. Narrative-only diffs don't count
    — see module docstring."""
    return bool(diff.provider_changes) or bool(diff.module_changes) or diff.resource_count_delta != 0


def _diff_one_repo(prior: RepoInventory, current: RepoInventory) -> RepoDiff:
    """Pair-wise diff between two snapshots of the SAME repo
    (`prior.meta.full_name == current.meta.full_name`)."""
    return RepoDiff(
        full_name=current.meta.full_name,
        provider_changes=_diff_providers(prior, current),
        module_changes=_diff_modules(prior, current),
        resource_count_delta=_resource_delta(prior, current),
        narrative_changed=_narrative_changed(prior, current),
    )


def _diff_providers(prior: RepoInventory, current: RepoInventory) -> list[ProviderChange]:
    prior_by_name = {p.name: p for p in prior.summary.providers}
    current_by_name = {p.name: p for p in current.summary.providers}
    added = [
        ProviderChange(
            name=name,
            change=ChangeType.ADDED,
            current_version=current_by_name[name].version,
        )
        for name in sorted(set(current_by_name) - set(prior_by_name))
    ]
    removed = [
        ProviderChange(
            name=name,
            change=ChangeType.REMOVED,
            prior_version=prior_by_name[name].version,
        )
        for name in sorted(set(prior_by_name) - set(current_by_name))
    ]
    changed = [
        ProviderChange(
            name=name,
            change=ChangeType.CHANGED,
            prior_version=prior_by_name[name].version,
            current_version=current_by_name[name].version,
        )
        for name in sorted(set(prior_by_name) & set(current_by_name))
        if prior_by_name[name].version != current_by_name[name].version
    ]
    return added + removed + changed


def _diff_modules(prior: RepoInventory, current: RepoInventory) -> list[ModuleChange]:
    # Index by (name, source) — same module pulled from two different
    # sources is two distinct entries. Renaming the local alias surfaces
    # as remove+add (we don't reach into git history to detect renames).
    prior_by_key = {(m.name, m.source): m for m in prior.summary.modules}
    current_by_key = {(m.name, m.source): m for m in current.summary.modules}
    added = [
        ModuleChange(
            name=current_by_key[key].name,
            source=current_by_key[key].source,
            change=ChangeType.ADDED,
            current_version=current_by_key[key].version,
        )
        for key in sorted(set(current_by_key) - set(prior_by_key))
    ]
    removed = [
        ModuleChange(
            name=prior_by_key[key].name,
            source=prior_by_key[key].source,
            change=ChangeType.REMOVED,
            prior_version=prior_by_key[key].version,
        )
        for key in sorted(set(prior_by_key) - set(current_by_key))
    ]
    changed = [
        ModuleChange(
            name=current_by_key[key].name,
            source=current_by_key[key].source,
            change=ChangeType.CHANGED,
            prior_version=prior_by_key[key].version,
            current_version=current_by_key[key].version,
        )
        for key in sorted(set(prior_by_key) & set(current_by_key))
        if prior_by_key[key].version != current_by_key[key].version
    ]
    return added + removed + changed


def _resource_delta(prior: RepoInventory, current: RepoInventory) -> int:
    prior_total = sum(prior.summary.resource_counts_by_type.values())
    current_total = sum(current.summary.resource_counts_by_type.values())
    return current_total - prior_total


def _narrative_changed(prior: RepoInventory, current: RepoInventory) -> bool:
    """True when the `purpose` paragraph differs. We compare only the
    purpose string — the other narrative fields (environments,
    owning_team_guess, notable_patterns) are derived from structural
    facts and tend to flap less, so they're not part of this signal."""
    prior_purpose = prior.narrative.purpose if prior.narrative else None
    current_purpose = current.narrative.purpose if current.narrative else None
    return prior_purpose != current_purpose


# ─── load ─────────────────────────────────────────────────────────────


def load_prior_inventories(path: Path | str) -> list[RepoInventory]:
    """Load `RepoInventory` records from a JSON-publisher output dir.

    Layout expected (matches what `LocalJsonPublisher` produces):

        <path>/
        ├── index.json             # ignored — diff uses per-repo files
        └── repos/
            ├── acme__main.json
            └── ...

    Per-repo file shape (from `render_child_json`):

        {
          "iac_cartographer": {...},   # banner metadata — ignored
          "meta": {...},                # → RepoMetadata
          "summary": {...},             # → TerraformSummary
          "narrative": {...} | null     # → BedrockNarrative | None
        }

    First-run shape: `<path>` doesn't exist or `<path>/repos/` is
    empty → returns `[]`. The diff then renders as all-additions
    (useful as a baseline summary rather than an error)."""
    base = Path(path)
    repos_dir = base / "repos"
    if not repos_dir.is_dir():
        logger.info("diff: prior-output dir %s has no repos/ subdir — treating as first run (empty prior)", base)
        return []

    inventories: list[RepoInventory] = []
    for child_path in sorted(repos_dir.glob("*.json")):
        try:
            payload = json.loads(child_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("diff: skipping unreadable prior file %s: %s", child_path, exc)
            continue
        # The JSON publisher wraps the inventory in an iac_cartographer
        # banner block + flat meta/summary/narrative fields. RepoInventory
        # expects just the latter three.
        try:
            inv = RepoInventory.model_validate(
                {
                    "meta": payload["meta"],
                    "summary": payload["summary"],
                    "narrative": payload.get("narrative"),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("diff: skipping malformed prior file %s: %s", child_path, exc)
            continue
        inventories.append(inv)

    logger.info("diff: loaded %d prior inventories from %s", len(inventories), repos_dir)
    return inventories


# ─── render ───────────────────────────────────────────────────────────


def render_diff_markdown(diff: InventoryDiff) -> str:
    """Render a Markdown diff summary, suitable for stdout, a Slack
    post, or appending to a static-site changelog.

    Shape:

        ## Inventory diff

        **Added (3):** acme/new-svc, acme/another-svc, acme/edge-cache
        **Removed (1):** acme/old-svc
        **Changed (2):**
          - acme/main-cluster: provider aws bumped (>=5.0 → >=6.0); +2 resources
          - acme/auth-service: module terraform-aws-vpc bumped (4.0.0 → 5.0.0)

        37 unchanged.

    When there are no changes at all, returns a one-line "No changes."
    summary — keeps the renderer's output stable enough for downstream
    grep / regex consumers."""
    if _is_empty(diff):
        return f"## Inventory diff\n\nNo changes. {diff.unchanged_count} repos tracked.\n"

    lines: list[str] = ["## Inventory diff", ""]

    if diff.added_repos:
        lines.append(f"**Added ({len(diff.added_repos)}):** {', '.join(diff.added_repos)}")
    if diff.removed_repos:
        lines.append(f"**Removed ({len(diff.removed_repos)}):** {', '.join(diff.removed_repos)}")
    if diff.changed_repos:
        lines.append(f"**Changed ({len(diff.changed_repos)}):**")
        lines.extend(f"  - {repo.full_name}: {_render_repo_changes(repo)}" for repo in diff.changed_repos)

    lines.append("")
    lines.append(f"{diff.unchanged_count} unchanged.")
    return "\n".join(lines) + "\n"


def render_diff_summary(diff: InventoryDiff) -> str:
    """One-line summary suitable for the end-of-run Slack `info` post.

    Shape: `"3 new repos, 1 archived, 2 changed; 37 unchanged"`. When
    everything is unchanged: `"no changes; N unchanged"`."""
    if _is_empty(diff):
        return f"no changes; {diff.unchanged_count} unchanged"
    parts: list[str] = []
    if diff.added_repos:
        parts.append(f"{len(diff.added_repos)} new")
    if diff.removed_repos:
        parts.append(f"{len(diff.removed_repos)} archived")
    if diff.changed_repos:
        parts.append(f"{len(diff.changed_repos)} changed")
    return f"{', '.join(parts)}; {diff.unchanged_count} unchanged"


def _is_empty(diff: InventoryDiff) -> bool:
    return not diff.added_repos and not diff.removed_repos and not diff.changed_repos


def _render_repo_changes(repo: RepoDiff) -> str:
    """One repo's change summary as a single inline string. Kept compact
    so the Markdown bullet line stays under ~120 chars in the common
    case (single provider bump + small resource delta)."""
    fragments: list[str] = [_render_provider_change(pc) for pc in repo.provider_changes]
    fragments.extend(_render_module_change(mc) for mc in repo.module_changes)
    if repo.resource_count_delta != 0:
        sign = "+" if repo.resource_count_delta > 0 else ""
        fragments.append(f"{sign}{repo.resource_count_delta} resources")
    return "; ".join(fragments)


def _render_provider_change(change: ProviderChange) -> str:
    if change.change == ChangeType.ADDED:
        v = change.current_version or "unpinned"
        return f"provider {change.name} added ({v})"
    if change.change == ChangeType.REMOVED:
        v = change.prior_version or "unpinned"
        return f"provider {change.name} removed (was {v})"
    # CHANGED
    old = change.prior_version or "unpinned"
    new = change.current_version or "unpinned"
    return f"provider {change.name} bumped ({old} → {new})"


def _render_module_change(change: ModuleChange) -> str:
    if change.change == ChangeType.ADDED:
        v = change.current_version or "unpinned"
        return f"module {change.name} added ({change.source} @ {v})"
    if change.change == ChangeType.REMOVED:
        v = change.prior_version or "unpinned"
        return f"module {change.name} removed (was {change.source} @ {v})"
    # CHANGED
    old = change.prior_version or "unpinned"
    new = change.current_version or "unpinned"
    return f"module {change.name} bumped ({old} → {new})"
