"""Tests for `iac_cartographer.overlays.live_state` (issue #98).

Covers the acceptance criteria for the main feature and the stale-apply
sub-feature:

  * Workspace found / not found / explicit mapping pattern match.
  * `LiveStateInfo` shape — current run, last successful apply, drift,
    live resource count.
  * Stale failed-apply alerts:
      - errored > threshold      → alert fires
      - errored < threshold      → no alert
      - errored, newer apply in flight → no alert (operator is on it)
      - errored but acknowledged_stale → no alert (muted)
  * `build_overlay` factory: none → no-op; tfc without org / token → error.
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
    TfcCredentials,
    WorkspaceMappingRule,
)
from iac_cartographer.overlays.live_state import (
    StaleAlertCollector,
    TFCOverlay,
    build_overlay,
)

TFC_HOST = "app.terraform.io"
TFC_ORG = "acme"
TFC_BASE = f"https://{TFC_HOST}/api/v2"


def _workspace_payload(ws_id: str, name: str, *, current_run_id: str | None = None) -> dict:
    return {
        "id": ws_id,
        "type": "workspaces",
        "attributes": {"name": name},
        "relationships": {"current-run": {"data": {"id": current_run_id, "type": "runs"} if current_run_id else None}},
    }


def _run_payload(run_id: str, *, status: str, applied_at: str | None = None, errored_at: str | None = None) -> dict:
    timestamps: dict[str, str] = {}
    if applied_at:
        timestamps["applied-at"] = applied_at
    if errored_at:
        timestamps["errored-at"] = errored_at
    return {
        "id": run_id,
        "type": "runs",
        "attributes": {"status": status, "status-timestamps": timestamps, "created-at": applied_at or errored_at or ""},
    }


def _list_workspaces_route(workspaces: list[dict]) -> None:
    respx.get(f"{TFC_BASE}/organizations/{TFC_ORG}/workspaces").mock(
        return_value=httpx.Response(200, json={"data": workspaces, "links": {"next": None}})
    )


def _overlay(
    *,
    mapping: list[WorkspaceMappingRule] | None = None,
    collector: StaleAlertCollector | None = None,
    threshold_days: int = 2,
    acknowledged_stale: list[str] | None = None,
) -> TFCOverlay:
    return TFCOverlay(
        hostname=TFC_HOST,
        organization=TFC_ORG,
        creds=TfcCredentials(token="t"),
        workspace_mapping=mapping,
        staleness_threshold_days=threshold_days,
        acknowledged_stale=acknowledged_stale,
        alert_collector=collector,
    )


# ─── Workspace resolution + happy-path fetch ──────────────────────────


@respx.mock
def test_workspace_found_returns_live_state_info() -> None:
    """Default heuristic: workspace name = last `/` segment of full_name."""
    _list_workspaces_route(
        [
            _workspace_payload("ws-1", "main-cluster", current_run_id="run-1"),
            _workspace_payload("ws-2", "other-thing"),
        ]
    )
    respx.get(f"{TFC_BASE}/runs/run-1").mock(
        return_value=httpx.Response(200, json={"data": _run_payload("run-1", status="applied")})
    )
    respx.get(f"{TFC_BASE}/workspaces/ws-1/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _run_payload("run-1", status="applied", applied_at="2026-06-01T12:00:00Z"),
                ]
            },
        )
    )
    respx.get(f"{TFC_BASE}/workspaces/ws-1/resources").mock(
        return_value=httpx.Response(200, json={"meta": {"pagination": {"total-count": 47}}})
    )

    with _overlay() as overlay:
        info = overlay.fetch("acme-org/main-cluster")
    assert info is not None
    assert info.workspace_name == "main-cluster"
    assert info.workspace_url == "https://app.terraform.io/app/acme/workspaces/main-cluster"
    assert info.current_run_status == "applied"
    assert info.current_run_id == "run-1"
    assert info.last_successful_apply_at == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    assert info.live_resource_count == 47
    assert info.drift_status == "not_configured"


@respx.mock
def test_workspace_not_found_returns_none() -> None:
    """An empty workspace list (or a non-matching name) returns `None`
    so the renderer simply omits the section — no error."""
    _list_workspaces_route([_workspace_payload("ws-1", "totally-other-name")])
    with _overlay() as overlay:
        info = overlay.fetch("acme-org/main-cluster")
    assert info is None


@respx.mock
def test_explicit_mapping_pattern_overrides_default_heuristic() -> None:
    """`workspace_mapping` first-match wins: a glob like `acme/*` can
    point an entire path family at a single TFC workspace."""
    _list_workspaces_route([_workspace_payload("ws-prod", "prod-app")])
    respx.get(f"{TFC_BASE}/workspaces/ws-prod/runs").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{TFC_BASE}/workspaces/ws-prod/resources").mock(
        return_value=httpx.Response(200, json={"meta": {"pagination": {"total-count": 0}}})
    )
    with _overlay(mapping=[WorkspaceMappingRule(repo="acme-org/prod-*", workspace="prod-app")]) as overlay:
        info = overlay.fetch("acme-org/prod-frontend")
    assert info is not None
    assert info.workspace_name == "prod-app"


@respx.mock
def test_drift_detected_attribute_surfaces() -> None:
    """`drifted: true` on the workspace attrs maps to `drift_detected`."""
    workspace = _workspace_payload("ws-1", "x")
    workspace["attributes"]["drifted"] = True
    _list_workspaces_route([workspace])
    respx.get(f"{TFC_BASE}/workspaces/ws-1/runs").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{TFC_BASE}/workspaces/ws-1/resources").mock(
        return_value=httpx.Response(200, json={"meta": {"pagination": {"total-count": 0}}})
    )
    with _overlay() as overlay:
        info = overlay.fetch("acme/x")
    assert info is not None
    assert info.drift_status == "drift_detected"


# ─── Stale-apply detection (sub-feature) ──────────────────────────────


def _stale_setup(*, runs: list[dict]) -> None:
    """Common setup for the stale-apply test cases — one workspace, the
    provided run history."""
    _list_workspaces_route([_workspace_payload("ws-1", "main-cluster")])
    respx.get(f"{TFC_BASE}/workspaces/ws-1/runs").mock(return_value=httpx.Response(200, json={"data": runs}))
    respx.get(f"{TFC_BASE}/workspaces/ws-1/resources").mock(
        return_value=httpx.Response(200, json={"meta": {"pagination": {"total-count": 0}}})
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@respx.mock
def test_stale_alert_fires_for_run_errored_longer_than_threshold() -> None:
    old = datetime.now(UTC) - timedelta(days=5)
    _stale_setup(runs=[_run_payload("run-bad", status="errored", errored_at=_iso(old))])

    collector = StaleAlertCollector()
    with _overlay(collector=collector, threshold_days=2) as overlay:
        overlay.fetch("acme/main-cluster")
    assert len(collector.alerts) == 1
    alert = collector.alerts[0]
    assert alert.workspace_name == "main-cluster"
    assert alert.failed_run_id == "run-bad"
    assert "run-bad" in alert.failed_run_url
    assert alert.days_in_state >= 4.9  # ~5 days within rounding


@respx.mock
def test_stale_alert_suppressed_when_below_threshold() -> None:
    recent = datetime.now(UTC) - timedelta(hours=6)
    _stale_setup(runs=[_run_payload("run-bad", status="errored", errored_at=_iso(recent))])

    collector = StaleAlertCollector()
    with _overlay(collector=collector, threshold_days=2) as overlay:
        overlay.fetch("acme/main-cluster")
    assert collector.alerts == []


@respx.mock
def test_stale_alert_suppressed_when_newer_apply_in_flight() -> None:
    """A newer `applying` (or any `_TFC_IN_FLIGHT_STATUSES` member)
    means the team is already on the fix — no alert."""
    old = datetime.now(UTC) - timedelta(days=5)
    _stale_setup(
        runs=[
            _run_payload("run-retry", status="applying"),
            _run_payload("run-bad", status="errored", errored_at=_iso(old)),
        ]
    )
    collector = StaleAlertCollector()
    with _overlay(collector=collector) as overlay:
        overlay.fetch("acme/main-cluster")
    assert collector.alerts == []


@respx.mock
def test_stale_alert_suppressed_when_workspace_acknowledged() -> None:
    """`acknowledged_stale` patterns mute alerts for known-broken
    workspaces (decommissioning queue, deferred work)."""
    old = datetime.now(UTC) - timedelta(days=10)
    _stale_setup(runs=[_run_payload("run-bad", status="errored", errored_at=_iso(old))])
    collector = StaleAlertCollector()
    with _overlay(collector=collector, threshold_days=2, acknowledged_stale=["main-*"]) as overlay:
        overlay.fetch("acme/main-cluster")
    assert collector.alerts == []


@respx.mock
def test_stale_alert_suppressed_when_newer_successful_apply_exists() -> None:
    """If the most-recent apply succeeded *after* the errored one, the
    workspace is healthy now — no alert even if the failure is old."""
    old = datetime.now(UTC) - timedelta(days=10)
    _stale_setup(
        runs=[
            _run_payload("run-good", status="applied", applied_at=_iso(datetime.now(UTC) - timedelta(days=1))),
            _run_payload("run-bad", status="errored", errored_at=_iso(old)),
        ]
    )
    collector = StaleAlertCollector()
    with _overlay(collector=collector) as overlay:
        overlay.fetch("acme/main-cluster")
    assert collector.alerts == []


# ─── build_overlay factory ────────────────────────────────────────────


def test_build_overlay_none_backend_returns_none() -> None:
    """The default no-op path — no credential needed, no API calls made."""
    assert build_overlay(LiveStateConfig(backend="none")) is None


def test_build_overlay_tfc_without_org_raises() -> None:
    with pytest.raises(CartographerError, match="organization is empty"):
        build_overlay(
            LiveStateConfig(backend="tfc", organization=""),
            tfc_creds=TfcCredentials(token="t"),
        )


def test_build_overlay_tfc_without_creds_raises() -> None:
    with pytest.raises(CartographerError, match="TfcCredentials"):
        build_overlay(LiveStateConfig(backend="tfc", organization="acme"))


def test_build_overlay_tfc_returns_tfc_overlay() -> None:
    overlay = build_overlay(
        LiveStateConfig(
            backend="tfc",
            organization="acme",
            staleness=StalenessConfig(enabled=False),
        ),
        tfc_creds=TfcCredentials(token="t"),
    )
    assert isinstance(overlay, TFCOverlay)
    overlay.close()


def test_build_overlay_passes_alert_collector_when_staleness_enabled() -> None:
    """When staleness is enabled, the collector reference flows through;
    when disabled, the overlay gets `None` and silently skips detection."""
    collector = StaleAlertCollector()
    on = build_overlay(
        LiveStateConfig(backend="tfc", organization="acme", staleness=StalenessConfig(enabled=True)),
        tfc_creds=TfcCredentials(token="t"),
        alert_collector=collector,
    )
    assert on is not None and on._alert_collector is collector  # type: ignore[union-attr]
    on.close()
    off = build_overlay(
        LiveStateConfig(backend="tfc", organization="acme", staleness=StalenessConfig(enabled=False)),
        tfc_creds=TfcCredentials(token="t"),
        alert_collector=collector,
    )
    assert off is not None and off._alert_collector is None  # type: ignore[union-attr]
    off.close()
