"""Tests for the Terrakube live-state overlay (issue #99).

Covers the acceptance criteria of #99:

  * Workspace found / not found / explicit mapping pattern match.
  * Drift status always `not_configured` (Terrakube has none at workspace
    level); renderer-side handling proven by the TFC tests, same shape here.
  * Stale failed-apply detection (Terrakube vocabulary):
      - `failed` > threshold      → alert fires
      - `failed` < threshold      → no alert
      - `failed`, newer in-flight → no alert (operator is on it)
      - `failed`, newer success   → no alert (already recovered)
      - workspace acknowledged    → no alert (muted)
      - jobs endpoint missing     → graceful degradation, overlay still renders
  * `build_overlay` factory: terrakube without org / hostname / creds → error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from iac_cartographer.constants import CartographerError
from iac_cartographer.models import (
    LiveStateConfig,
    StalenessConfig,
    TerrakubeCredentials,
    WorkspaceMappingRule,
)
from iac_cartographer.overlays.live_state import (
    StaleAlertCollector,
    TerrakubeOverlay,
    build_overlay,
)

TK_HOST = "terrakube.example.com"
TK_ORG_NAME = "acme"
TK_ORG_ID = "11111111-1111-1111-1111-111111111111"
TK_BASE = f"https://{TK_HOST}/api/v1"


def _workspace_payload(ws_id: str, name: str) -> dict:
    return {
        "id": ws_id,
        "type": "workspace",
        "attributes": {"name": name},
    }


def _job_payload(job_id: str, *, status: str, updated_date: str | None = None) -> dict:
    attrs: dict[str, str] = {"status": status}
    if updated_date is not None:
        attrs["updatedDate"] = updated_date
        attrs["createdDate"] = updated_date
    return {
        "id": job_id,
        "type": "job",
        "attributes": attrs,
    }


def _route_org_lookup() -> None:
    respx.get(f"{TK_BASE}/organization").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": TK_ORG_ID,
                        "type": "organization",
                        "attributes": {"name": TK_ORG_NAME},
                    }
                ],
                "links": {"next": None},
            },
        )
    )


def _route_workspace_list(workspaces: list[dict]) -> None:
    respx.get(f"{TK_BASE}/organization/{TK_ORG_ID}/workspace").mock(
        return_value=httpx.Response(200, json={"data": workspaces, "links": {"next": None}})
    )


def _route_workspace_jobs(workspace_id: str, jobs: list[dict]) -> None:
    respx.get(f"{TK_BASE}/workspace/{workspace_id}/job").mock(
        return_value=httpx.Response(200, json={"data": jobs, "links": {"next": None}})
    )


def _overlay(
    *,
    mapping: list[WorkspaceMappingRule] | None = None,
    collector: StaleAlertCollector | None = None,
    threshold_days: int = 2,
    acknowledged_stale: list[str] | None = None,
) -> TerrakubeOverlay:
    return TerrakubeOverlay(
        hostname=TK_HOST,
        organization=TK_ORG_NAME,
        creds=TerrakubeCredentials(token="pat"),
        workspace_mapping=mapping,
        staleness_threshold_days=threshold_days,
        acknowledged_stale=acknowledged_stale,
        alert_collector=collector,
    )


# ─── Workspace resolution + happy-path fetch ──────────────────────────


@respx.mock
def test_workspace_found_returns_live_state_info() -> None:
    """Default heuristic: workspace name = last `/` segment of full_name."""
    _route_org_lookup()
    _route_workspace_list(
        [
            _workspace_payload("ws-1", "main-cluster"),
            _workspace_payload("ws-2", "other-thing"),
        ]
    )
    _route_workspace_jobs(
        "ws-1",
        [
            _job_payload("j-99", status="completed", updated_date="2026-05-30T12:00:00.000Z"),
        ],
    )
    with _overlay() as overlay:
        info = overlay.fetch("acme-org/main-cluster")
    assert info is not None
    assert info.workspace_name == "main-cluster"
    assert info.workspace_url == f"https://{TK_HOST}/organizations/{TK_ORG_NAME}/workspaces/main-cluster"
    assert info.current_run_id == "j-99"
    assert info.current_run_status == "completed"
    assert info.current_run_url is not None and "j-99" in info.current_run_url
    assert info.last_successful_apply_at is not None
    # Drift is never surfaced on Terrakube workspaces — always neutral.
    assert info.drift_status == "not_configured"
    # No resource-count endpoint on Terrakube — overlay leaves this None.
    assert info.live_resource_count is None


@respx.mock
def test_workspace_not_found_returns_none() -> None:
    """Workspace name resolves but the org doesn't have it."""
    _route_org_lookup()
    _route_workspace_list([_workspace_payload("ws-2", "other-thing")])
    with _overlay() as overlay:
        info = overlay.fetch("acme-org/main-cluster")
    assert info is None


@respx.mock
def test_explicit_mapping_overrides_default_heuristic() -> None:
    """An explicit mapping rule beats the last-segment heuristic."""
    _route_org_lookup()
    _route_workspace_list([_workspace_payload("ws-9", "prod-platform")])
    _route_workspace_jobs(
        "ws-9",
        [_job_payload("j-1", status="completed", updated_date="2026-05-30T12:00:00.000Z")],
    )
    rule = WorkspaceMappingRule(repo="acme-org/main-*", workspace="prod-platform")
    with _overlay(mapping=[rule]) as overlay:
        info = overlay.fetch("acme-org/main-cluster")
    assert info is not None
    assert info.workspace_name == "prod-platform"


@respx.mock
def test_no_changes_status_counts_as_successful_apply() -> None:
    """Terrakube emits `noChanges` for a no-op apply — we treat that as
    a successful apply for last-apply / stale detection purposes."""
    _route_org_lookup()
    _route_workspace_list([_workspace_payload("ws-1", "main-cluster")])
    _route_workspace_jobs(
        "ws-1",
        [_job_payload("j-1", status="noChanges", updated_date="2026-05-30T12:00:00.000Z")],
    )
    with _overlay() as overlay:
        info = overlay.fetch("acme-org/main-cluster")
    assert info is not None
    assert info.last_successful_apply_at is not None


@respx.mock
def test_jobs_endpoint_failure_degrades_gracefully() -> None:
    """When the jobs endpoint isn't available (older Terrakube, network
    blip, 5xx), the overlay still returns a `LiveStateInfo` with the
    workspace name/URL — just no run info, no last-apply, no stale check."""
    _route_org_lookup()
    _route_workspace_list([_workspace_payload("ws-1", "main-cluster")])
    respx.get(f"{TK_BASE}/workspace/ws-1/job").mock(return_value=httpx.Response(404))
    with _overlay() as overlay:
        info = overlay.fetch("acme-org/main-cluster")
    assert info is not None
    assert info.workspace_name == "main-cluster"
    assert info.current_run_id is None
    assert info.current_run_status is None
    assert info.last_successful_apply_at is None


@respx.mock
def test_invalid_token_lookup_returns_none() -> None:
    """A 401 on org lookup is treated as "overlay unavailable" — log + None,
    don't sink the rest of the pipeline."""
    respx.get(f"{TK_BASE}/organization").mock(return_value=httpx.Response(401))
    with _overlay() as overlay:
        info = overlay.fetch("acme-org/main-cluster")
    assert info is None


# ─── Stale failed-apply detection ─────────────────────────────────────


@respx.mock
def test_stale_alert_fires_for_failed_apply_above_threshold() -> None:
    _route_org_lookup()
    _route_workspace_list([_workspace_payload("ws-1", "main-cluster")])
    failed_at = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _route_workspace_jobs(
        "ws-1",
        [_job_payload("j-failed", status="failed", updated_date=failed_at)],
    )
    collector = StaleAlertCollector()
    with _overlay(collector=collector, threshold_days=2) as overlay:
        overlay.fetch("acme-org/main-cluster")
    assert len(collector.alerts) == 1
    alert = collector.alerts[0]
    assert alert.workspace_name == "main-cluster"
    assert alert.failed_run_id == "j-failed"
    assert alert.days_in_state >= 2
    assert alert.failed_run_url.endswith("/runs/j-failed")


@respx.mock
def test_stale_alert_suppressed_below_threshold() -> None:
    _route_org_lookup()
    _route_workspace_list([_workspace_payload("ws-1", "main-cluster")])
    failed_at = (datetime.now(UTC) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _route_workspace_jobs(
        "ws-1",
        [_job_payload("j-failed", status="failed", updated_date=failed_at)],
    )
    collector = StaleAlertCollector()
    with _overlay(collector=collector, threshold_days=2) as overlay:
        overlay.fetch("acme-org/main-cluster")
    assert collector.alerts == []


@respx.mock
def test_stale_alert_suppressed_when_newer_run_in_flight() -> None:
    """A newer apply is queued / running — operator is on it, don't alert."""
    _route_org_lookup()
    _route_workspace_list([_workspace_payload("ws-1", "main-cluster")])
    failed_at = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    newer = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _route_workspace_jobs(
        "ws-1",
        [
            _job_payload("j-running", status="running", updated_date=newer),
            _job_payload("j-failed", status="failed", updated_date=failed_at),
        ],
    )
    collector = StaleAlertCollector()
    with _overlay(collector=collector, threshold_days=2) as overlay:
        overlay.fetch("acme-org/main-cluster")
    assert collector.alerts == []


@respx.mock
def test_stale_alert_suppressed_when_newer_successful_apply_exists() -> None:
    """An old failure is fine if a newer apply succeeded — the workspace
    is healthy now."""
    _route_org_lookup()
    _route_workspace_list([_workspace_payload("ws-1", "main-cluster")])
    failed_at = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    newer = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _route_workspace_jobs(
        "ws-1",
        [
            _job_payload("j-ok", status="completed", updated_date=newer),
            _job_payload("j-failed", status="failed", updated_date=failed_at),
        ],
    )
    collector = StaleAlertCollector()
    with _overlay(collector=collector, threshold_days=2) as overlay:
        overlay.fetch("acme-org/main-cluster")
    assert collector.alerts == []


@respx.mock
def test_stale_alert_suppressed_when_workspace_acknowledged() -> None:
    """The `acknowledged_stale` list mutes specific workspaces by pattern."""
    _route_org_lookup()
    _route_workspace_list([_workspace_payload("ws-1", "main-cluster")])
    failed_at = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _route_workspace_jobs(
        "ws-1",
        [_job_payload("j-failed", status="failed", updated_date=failed_at)],
    )
    collector = StaleAlertCollector()
    with _overlay(collector=collector, threshold_days=2, acknowledged_stale=["main-*"]) as overlay:
        overlay.fetch("acme-org/main-cluster")
    assert collector.alerts == []


# ─── build_overlay factory ────────────────────────────────────────────


def test_build_overlay_terrakube_without_org_raises() -> None:
    with pytest.raises(CartographerError, match="organization is empty"):
        build_overlay(
            LiveStateConfig(
                backend="terrakube",
                organization="",
                hostname=TK_HOST,
            ),
            terrakube_creds=TerrakubeCredentials(token="t"),
        )


def test_build_overlay_terrakube_without_hostname_raises() -> None:
    with pytest.raises(CartographerError, match="hostname"):
        build_overlay(
            LiveStateConfig(backend="terrakube", organization="acme"),
            terrakube_creds=TerrakubeCredentials(token="t"),
        )


def test_build_overlay_terrakube_without_creds_raises() -> None:
    with pytest.raises(CartographerError, match="TerrakubeCredentials"):
        build_overlay(
            LiveStateConfig(
                backend="terrakube",
                organization="acme",
                hostname=TK_HOST,
            )
        )


def test_build_overlay_terrakube_returns_terrakube_overlay() -> None:
    overlay = build_overlay(
        LiveStateConfig(
            backend="terrakube",
            organization="acme",
            hostname=TK_HOST,
            staleness=StalenessConfig(enabled=False),
        ),
        terrakube_creds=TerrakubeCredentials(token="t"),
    )
    assert isinstance(overlay, TerrakubeOverlay)
    overlay.close()


def test_build_overlay_terrakube_threads_alert_collector_when_staleness_enabled() -> None:
    collector = StaleAlertCollector()
    on = build_overlay(
        LiveStateConfig(
            backend="terrakube",
            organization="acme",
            hostname=TK_HOST,
            staleness=StalenessConfig(enabled=True),
        ),
        terrakube_creds=TerrakubeCredentials(token="t"),
        alert_collector=collector,
    )
    assert on is not None and on._alert_collector is collector  # type: ignore[union-attr]
    on.close()
    off = build_overlay(
        LiveStateConfig(
            backend="terrakube",
            organization="acme",
            hostname=TK_HOST,
            staleness=StalenessConfig(enabled=False),
        ),
        terrakube_creds=TerrakubeCredentials(token="t"),
        alert_collector=collector,
    )
    assert off is not None and off._alert_collector is None  # type: ignore[union-attr]
    off.close()
