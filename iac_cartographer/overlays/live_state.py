"""Live-state overlay — Terraform Cloud / HCP / Terraform Enterprise.

Layers workspace info (current run status, last successful apply, drift,
live resource count) from the platform's API on top of the static
inventory the rest of the pipeline builds. Read-only: the overlay only
ever issues GETs.

Protocol
--------
`LiveStateOverlay.fetch(repo_full_name)` returns one `LiveStateInfo` for
the workspace mapped to the repo, or `None` if no mapping resolves. The
abstraction is what issue #99 (Terrakube) reuses — Terrakube's API
surface is similar enough that a second class implementing the same
protocol can ship on top of this without touching the renderer /
orchestrator / diagnose paths.

Mapping
-------
`config.live_state.workspace_mapping` is a list of `WorkspaceMappingRule`
entries (`repo:` + `workspace:`, both fnmatch-style). First-match wins.
Empty list falls back to a default heuristic: workspace name = last
segment of `repo.full_name` (so `acme-org/main-cluster` → `main-cluster`).
That covers the dominant convention with no config; explicit rules are
for repos whose workspace names don't follow it.

Stale failed-apply detection
----------------------------
Free side-effect of fetching workspace state: we already know whether
the most recent apply failed and how long ago that was. When the gap
exceeds `staleness.threshold_days`, the overlay appends a `StaleApplyAlert`
to its shared `StaleAlertCollector`. The orchestrator drains the
collector at the end of the run and dispatches each alert via the
existing notifications channel set at `warn` level. Workspaces with a
newer in-flight successful apply, or matched by `acknowledged_stale`,
are skipped silently.

TFC API endpoints used
----------------------
* `GET /api/v2/organizations/{org}/workspaces?page[size]=100`
  Paginated list of all workspaces in the org — used to resolve a
  workspace name to its TFC ID + warm a per-overlay cache.
* `GET /api/v2/workspaces/{id}` — workspace details (current run ref,
  drift status if assessment-results were attached).
* `GET /api/v2/workspaces/{id}/runs?page[size]=20` — recent runs;
  scanned for "last successful apply" timestamp + stale-failed-apply
  detection.
* `GET /api/v2/workspaces/{id}/resources?page[size]=1` — live resource
  count via the JSON:API `meta.pagination.total-count` field.

Auth is a Bearer token (`Authorization: Bearer <token>`) provided by
`secrets.tfc.token`. Only `read` scopes are required.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from iac_cartographer.constants import CartographerError
from iac_cartographer.models import (
    LiveStateInfo,
    StaleApplyAlert,
)

if TYPE_CHECKING:
    from iac_cartographer.models import (
        LiveStateConfig,
        TfcCredentials,
        WorkspaceMappingRule,
    )

logger = logging.getLogger("iac_cartographer.overlays.live_state")

# TFC's run statuses indicating a non-terminal apply phase. Used to skip
# stale-alert emission when a newer apply is already in flight (the team
# is on it).
_TFC_IN_FLIGHT_STATUSES = frozenset(
    {
        "pending",
        "plan_queued",
        "planning",
        "planned",
        "cost_estimating",
        "cost_estimated",
        "policy_checking",
        "policy_override",
        "policy_checked",
        "apply_queued",
        "applying",
        "confirmed",
    }
)

# How aggressively to cap each TFC HTTP call. Live-state probing is a
# best-effort enrichment — if TFC is slow we'd rather skip the overlay
# for that repo than stall the pipeline. Per-call cap; per-repo budget
# is up to roughly 4x this (workspace lookup + workspace details + runs
# list + resource count).
_TFC_HTTP_TIMEOUT_S = 10.0


class LiveStateOverlay(Protocol):
    """Read-only view of an external state platform.

    Implementations: `TFCOverlay` (this module); future `TerrakubeOverlay`
    in #99. Both speak the same protocol so the orchestrator and renderer
    don't care which backend's behind it.
    """

    def fetch(self, repo_full_name: str) -> LiveStateInfo | None:
        """Return live state for `repo_full_name`, or `None` when no
        workspace maps to it. Implementations must never raise on
        per-repo failures — they log and return `None` so one bad
        workspace doesn't sink the rest of the run.
        """


@dataclass
class StaleAlertCollector:
    """Mutable list of stale-apply alerts, shared across `fetch()` calls.

    The overlay appends to it during each `fetch()`; the orchestrator
    drains it at the end of the run. Single-threaded (the pipeline
    processes repos serially with respect to the overlay), so no lock
    is needed.
    """

    alerts: list[StaleApplyAlert] = field(default_factory=list)

    def append(self, alert: StaleApplyAlert) -> None:
        self.alerts.append(alert)


# ─── TFC implementation ────────────────────────────────────────────────


class TFCOverlay:
    """Terraform Cloud / HCP / Terraform Enterprise overlay.

    Constructor wires a long-lived `httpx.Client` for connection reuse;
    call `close()` (or use as a context manager) when done. Pass a
    `StaleAlertCollector` to enable stale-apply detection; pass `None`
    to skip it entirely (the overlay still returns `LiveStateInfo` for
    rendered pages).
    """

    def __init__(
        self,
        *,
        hostname: str,
        organization: str,
        creds: TfcCredentials,
        workspace_mapping: list[WorkspaceMappingRule] | None = None,
        staleness_threshold_days: int = 2,
        acknowledged_stale: list[str] | None = None,
        alert_collector: StaleAlertCollector | None = None,
    ) -> None:
        self._hostname = hostname.rstrip("/")
        self._base_url = f"https://{self._hostname}/api/v2"
        self._org = organization
        self._mapping = list(workspace_mapping or [])
        self._staleness_threshold_days = staleness_threshold_days
        self._acknowledged_stale = list(acknowledged_stale or [])
        self._alert_collector = alert_collector
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=_TFC_HTTP_TIMEOUT_S,
            headers={
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/vnd.api+json",
                "User-Agent": "iac-cartographer",
            },
        )
        # Workspace cache, populated lazily on first lookup. Keyed by
        # name → workspace JSON. Built from a paginated org-wide list so
        # one TFC API roundtrip serves every repo in the run.
        self._workspaces: dict[str, dict[str, Any]] | None = None

    # ── Context-manager surface ─────────────────────────────────────

    def __enter__(self) -> TFCOverlay:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── Protocol implementation ──────────────────────────────────────

    def fetch(self, repo_full_name: str) -> LiveStateInfo | None:
        workspace_name = self._resolve_workspace_name(repo_full_name)
        if workspace_name is None:
            return None
        try:
            workspace = self._workspace_by_name(workspace_name)
        except Exception:
            logger.warning(
                "live_state[tfc]: workspace lookup failed for %s → %s — overlay skipped",
                repo_full_name,
                workspace_name,
                exc_info=True,
            )
            return None
        if workspace is None:
            logger.info(
                "live_state[tfc]: no TFC workspace named %r in org %r — overlay skipped for %s",
                workspace_name,
                self._org,
                repo_full_name,
            )
            return None

        workspace_id = workspace["id"]
        attrs = workspace.get("attributes", {}) or {}
        workspace_url = self._workspace_url(workspace_name)

        # Current run reference comes from the workspace's relationships
        # block. Some workspaces don't have a current run (newly created,
        # paused) — that's fine; `current_run_*` stays None.
        current_run_id: str | None = None
        current_run_status: str | None = None
        current_run_url: str | None = None
        current_run_rel = (workspace.get("relationships") or {}).get("current-run") or {}
        current_run_data = current_run_rel.get("data") or {}
        if isinstance(current_run_data, dict) and current_run_data.get("id"):
            current_run_id = current_run_data["id"]
            current_run_url = (
                f"https://{self._hostname}/app/{self._org}/workspaces/{workspace_name}/runs/{current_run_id}"
            )
            try:
                current_run_status = self._fetch_run_status(current_run_id)
            except Exception:
                logger.debug(
                    "live_state[tfc]: current-run status fetch failed for %s — leaving status None",
                    workspace_name,
                    exc_info=True,
                )

        # Recent runs — the source of "last successful apply" + the
        # stale-failed-apply signal. One call returns up to 20 runs
        # (default page size), which always covers the lookback we
        # need.
        recent_runs = self._fetch_recent_runs(workspace_id)
        last_apply = _last_successful_apply_at(recent_runs)

        # Stale-apply detection. Skip the overlay's collector entirely
        # when the caller didn't pass one (tests, or runs with the
        # sub-feature disabled).
        if self._alert_collector is not None:
            alert = _detect_stale_alert(
                workspace_name=workspace_name,
                workspace_url=workspace_url,
                hostname=self._hostname,
                organization=self._org,
                recent_runs=recent_runs,
                threshold_days=self._staleness_threshold_days,
                acknowledged_stale=self._acknowledged_stale,
                last_successful_apply_at=last_apply,
            )
            if alert is not None:
                self._alert_collector.append(alert)

        # Drift status from TFC's assessment-results. The attribute name
        # on the workspace JSON has shifted over TFC versions; tolerate
        # both `drifted` (boolean) and the older `assessment-results`
        # relationship form.
        drift_status = _drift_status_from_workspace(attrs)

        # Live resource count via JSON:API total-count header. Cheap —
        # we ask for page[size]=1 and read the meta.
        live_resource_count = self._fetch_resource_count(workspace_id)

        return LiveStateInfo(
            workspace_name=workspace_name,
            workspace_url=workspace_url,
            current_run_status=current_run_status,
            current_run_id=current_run_id,
            current_run_url=current_run_url,
            last_successful_apply_at=last_apply,
            drift_status=drift_status,
            live_resource_count=live_resource_count,
        )

    # ── Mapping resolution ──────────────────────────────────────────

    def _resolve_workspace_name(self, repo_full_name: str) -> str | None:
        """First explicit `workspace_mapping` rule that matches; otherwise
        the default heuristic (`workspace = last segment of repo full_name`)."""
        for rule in self._mapping:
            if fnmatch.fnmatchcase(repo_full_name, rule.repo):
                return rule.workspace
        # Default heuristic: use the last `/`-segment.
        if "/" in repo_full_name:
            return repo_full_name.rsplit("/", 1)[1]
        return repo_full_name

    def _workspace_url(self, workspace_name: str) -> str:
        return f"https://{self._hostname}/app/{self._org}/workspaces/{workspace_name}"

    # ── TFC API calls ──────────────────────────────────────────────

    def _workspace_by_name(self, name: str) -> dict[str, Any] | None:
        """Return the workspace JSON for `name`, or `None` if it doesn't
        exist in the configured org. Lazily fills the per-overlay cache
        on first call; subsequent lookups are O(1)."""
        if self._workspaces is None:
            self._workspaces = self._fetch_all_workspaces()
        return self._workspaces.get(name)

    def _fetch_all_workspaces(self) -> dict[str, dict[str, Any]]:
        """List every workspace in the org and key by name. Paginated
        per JSON:API; we follow `links.next` until exhausted (capped at
        a sane number of pages so a misbehaving API can't loop forever)."""
        out: dict[str, dict[str, Any]] = {}
        url: str | None = f"/organizations/{self._org}/workspaces?page%5Bsize%5D=100"
        page = 0
        while url and page < 50:  # 50 pages x 100 = 5000 workspaces, plenty
            resp = self._client.get(url)
            resp.raise_for_status()
            body = resp.json()
            for ws in body.get("data", []) or []:
                attrs = ws.get("attributes", {}) or {}
                name = attrs.get("name")
                if isinstance(name, str):
                    out[name] = ws
            next_link = (body.get("links") or {}).get("next")
            url = _relative_link(next_link, self._base_url) if isinstance(next_link, str) else None
            page += 1
        return out

    def _fetch_run_status(self, run_id: str) -> str | None:
        resp = self._client.get(f"/runs/{run_id}")
        resp.raise_for_status()
        attrs = (resp.json().get("data") or {}).get("attributes") or {}
        status = attrs.get("status")
        return status if isinstance(status, str) else None

    def _fetch_recent_runs(self, workspace_id: str) -> list[dict[str, Any]]:
        try:
            resp = self._client.get(f"/workspaces/{workspace_id}/runs?page%5Bsize%5D=20")
            resp.raise_for_status()
        except Exception:
            logger.debug(
                "live_state[tfc]: recent-runs fetch failed for workspace_id=%s — empty list",
                workspace_id,
                exc_info=True,
            )
            return []
        data = resp.json().get("data") or []
        return [r for r in data if isinstance(r, dict)]

    def _fetch_resource_count(self, workspace_id: str) -> int | None:
        try:
            resp = self._client.get(f"/workspaces/{workspace_id}/resources?page%5Bsize%5D=1")
            resp.raise_for_status()
        except Exception:
            logger.debug(
                "live_state[tfc]: resource-count fetch failed for workspace_id=%s",
                workspace_id,
                exc_info=True,
            )
            return None
        meta = (resp.json().get("meta") or {}).get("pagination") or {}
        count = meta.get("total-count")
        return int(count) if isinstance(count, int) else None


# ─── helpers ───────────────────────────────────────────────────────────


def _relative_link(href: str, base_url: str) -> str:
    """TFC's `links.next` is sometimes absolute, sometimes relative. Strip
    the base URL when present so httpx applies its base URL correctly."""
    if href.startswith(base_url):
        return href[len(base_url) :]
    if href.startswith("http"):
        # Different host — pass through; httpx will fetch the absolute URL.
        return href
    return href


def _last_successful_apply_at(recent_runs: list[dict[str, Any]]) -> datetime | None:
    """Walk the recent-runs list (newest-first per TFC's API ordering)
    looking for the first run with status `applied`. The `applied-at`
    attribute on that run is the timestamp we surface."""
    for run in recent_runs:
        attrs = run.get("attributes") or {}
        if attrs.get("status") != "applied":
            continue
        ts = attrs.get("status-timestamps", {}).get("applied-at")
        if ts is None:
            ts = attrs.get("updated-at") or attrs.get("created-at")
        return _parse_iso(ts) if isinstance(ts, str) else None
    return None


def _drift_status_from_workspace(attrs: dict[str, Any]) -> str:
    """Map TFC's workspace-attributes shape to our three-state string.

    Newer TFC versions surface `drifted: bool` directly on the workspace
    attributes; older ones surface assessment data via a relationship
    that we don't follow (one extra API call per workspace we'd rather
    not pay for a feature few users have enabled). When the field is
    absent we return `"not_configured"` so the renderer shows neutral
    copy instead of guessing."""
    drifted = attrs.get("drifted")
    if drifted is True:
        return "drift_detected"
    if drifted is False:
        return "no_drift"
    return "not_configured"


def _detect_stale_alert(
    *,
    workspace_name: str,
    workspace_url: str,
    hostname: str,
    organization: str,
    recent_runs: list[dict[str, Any]],
    threshold_days: int,
    acknowledged_stale: list[str],
    last_successful_apply_at: datetime | None,
) -> StaleApplyAlert | None:
    """Look at the most-recent apply attempt; if it `errored` more than
    `threshold_days` ago AND no newer non-terminal apply has started
    since, return an alert.

    Skip rules (any one is sufficient):

      * Workspace name matches an `acknowledged_stale` pattern.
      * The most-recent run isn't in `errored` state (no failed apply
        to alert on).
      * A newer run is already in flight (planning / applying / queued /
        etc.) — the team is on it.
      * The failed-apply gap is below the threshold.
    """
    if any(fnmatch.fnmatchcase(workspace_name, pat) for pat in acknowledged_stale):
        return None

    # Find the most recent run that reached an apply phase (errored or
    # applied). Earlier runs in any other status (discarded plans,
    # policy checks) don't count.
    failed_run: dict[str, Any] | None = None
    for run in recent_runs:
        attrs = run.get("attributes") or {}
        status = attrs.get("status")
        if status == "errored":
            failed_run = run
            break
        if status == "applied":
            # Newer-or-equal successful run beats any older failure.
            return None
        if status in _TFC_IN_FLIGHT_STATUSES:
            # A newer apply is queued or running — operator is on it.
            return None

    if failed_run is None:
        return None

    failed_attrs = failed_run.get("attributes") or {}
    failed_ts = failed_attrs.get("status-timestamps", {}).get("errored-at")
    if failed_ts is None:
        failed_ts = failed_attrs.get("updated-at") or failed_attrs.get("created-at")
    failed_at = _parse_iso(failed_ts) if isinstance(failed_ts, str) else None
    if failed_at is None:
        return None

    gap_seconds = (datetime.now(UTC) - failed_at).total_seconds()
    days_in_state = gap_seconds / 86400.0
    if days_in_state < threshold_days:
        return None

    failed_run_id = failed_run.get("id", "")
    return StaleApplyAlert(
        workspace_name=workspace_name,
        workspace_url=workspace_url,
        failed_run_id=failed_run_id,
        failed_run_url=f"https://{hostname}/app/{organization}/workspaces/{workspace_name}/runs/{failed_run_id}",
        days_in_state=round(days_in_state, 2),
        last_successful_apply_at=last_successful_apply_at,
    )


def _parse_iso(ts: str) -> datetime | None:
    """TFC emits RFC 3339 timestamps with millisecond precision and a
    `Z` suffix. `datetime.fromisoformat` handles both that form and the
    less-common `+00:00` form. Return `None` on anything else so the
    overlay degrades to "no timestamp" rather than raising."""
    try:
        # Normalise the `Z` suffix that `fromisoformat` rejects pre-3.11.
        normalised = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ─── Construction ──────────────────────────────────────────────────────


def build_overlay(
    live_state: LiveStateConfig,
    creds: TfcCredentials | None,
    *,
    alert_collector: StaleAlertCollector | None = None,
) -> LiveStateOverlay | None:
    """Build the configured overlay. Returns `None` when
    `live_state.backend == "none"` (the no-op default).

    Raises `CartographerError` when an overlay is requested but the
    config is incomplete (missing organisation, missing credential).
    The orchestrator catches that in the same way it handles other
    startup-config errors.
    """
    if live_state.backend == "none":
        return None
    if live_state.backend == "tfc":
        if not live_state.organization:
            raise CartographerError(
                "live_state.backend=tfc but live_state.organization is empty — "
                "set it to the TFC / HCP / TFE organisation that owns the workspaces."
            )
        if creds is None:
            raise CartographerError(
                "live_state.backend=tfc but no TfcCredentials were loaded (check the iac-cartographer/tfc secret)"
            )
        return TFCOverlay(
            hostname=live_state.hostname,
            organization=live_state.organization,
            creds=creds,
            workspace_mapping=live_state.workspace_mapping,
            staleness_threshold_days=live_state.staleness.threshold_days,
            acknowledged_stale=live_state.staleness.acknowledged_stale if live_state.staleness.enabled else [],
            alert_collector=alert_collector if live_state.staleness.enabled else None,
        )
    raise CartographerError(f"unknown live_state.backend: {live_state.backend!r}")
