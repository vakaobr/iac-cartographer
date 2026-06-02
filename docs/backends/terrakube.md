# Live-state overlay: Terrakube

[Terrakube](https://docs.terrakube.io/) is a self-hostable, open-source
Terraform / OpenTofu state-and-runs platform. The Terrakube overlay
layers workspace info from a Terrakube instance on top of the static
inventory iac-cartographer builds — same shape as the
[Terraform Cloud overlay](../reference/configuration.md#live_state),
different backend.

Opt-in via `live_state.backend: terrakube`. Default is `none`; no
credential, no API calls, no behaviour change.

## Setup

```yaml
live_state:
  backend: terrakube
  organization: acme                       # Terrakube org NAME
  hostname: terrakube.example.com          # your install
  # workspace_mapping + staleness use the same shape as the TFC overlay.
  workspace_mapping:
    - repo: "acme-org/main-cluster"
      workspace: "prod-platform"
  staleness:
    enabled: true
    threshold_days: 2
    acknowledged_stale: []
```

`organization` is the human-readable Terrakube organisation NAME (the
overlay resolves it to the internal UUID at first lookup and caches it
for the rest of the run). `hostname` is the bare host — the overlay
constructs `https://<hostname>/api/v1/...` from it.

### Credential

A Terrakube Personal Access Token with read access to the configured
organisation. Stored under the logical name `iac-cartographer/terrakube`
via whichever [secrets backend](secrets.md) you're using:

```bash
# env backend
export IAC_CARTOGRAPHER_SECRET_TERRAKUBE='{"token":"<terrakube-pat>"}'

# AWS Secrets Manager (default backend)
aws secretsmanager create-secret \
  --name iac-cartographer/terrakube \
  --secret-string '{"token":"<terrakube-pat>"}'

# HashiCorp Vault
vault kv put secret/iac-cartographer/terrakube token=<terrakube-pat>
```

The overlay only ever issues GET requests — no write or admin scopes
required.

## What the overlay surfaces on each child page

For every rendered repo, the overlay looks up the corresponding Terrakube
workspace and renders these fields on the child page (under "Live state"):

| Field | Source | Notes |
|---|---|---|
| Workspace name + URL | `/organization/{orgId}/workspace` | URL points at the workspace in the Terrakube UI. |
| Current run | most-recent job from `/workspace/{wsId}/job` | One of `pending`, `waitingApproval`, `approved`, `queue`, `running`, `completed`, `noChanges`, `notExecuted`, `rejected`, `cancelled`, `failed`, `unknown`. |
| Last successful apply | first job with `status ∈ {completed, noChanges}` | Timestamp from `updatedDate`. |
| Drift | — | Terrakube has no workspace-level drift attribute. The renderer always shows `not configured`. |
| Live resource count | — | Terrakube has no `/workspaces/{id}/resources` total-count endpoint. The renderer omits the divergence row. |

The default repo → workspace mapping is "last segment of `repo.full_name`":
`acme-org/main-cluster` → `main-cluster`. Override via the
`workspace_mapping` list (first match wins, fnmatch-style globs).

## Stale failed-apply alerts

Same sub-feature as the TFC overlay: when the most-recent attempted
apply has `status: failed` for more than `staleness.threshold_days`,
the overlay raises a `warn`-level alert through the
[notifications dispatcher](notifications.md). Suppressed when:

* A newer job is in flight (`pending`, `waitingApproval`, `approved`,
  `queue`, `running`) — the operator is already on it.
* A newer job succeeded (`completed`, `noChanges`) — the workspace
  recovered.
* The workspace name matches an `acknowledged_stale` fnmatch pattern.

Set `staleness.enabled: false` to disable the alert path entirely
without touching the rest of the overlay.

## `--diagnose` integration

`iac-cartographer --diagnose` emits two rows for the Terrakube path:

| Check | Offline | `--live` |
|---|---|---|
| `live-state` | validates `organization` + `hostname` non-default | — |
| `secrets.terrakube` | marked `ok — required by live_state.backend=terrakube` | — |
| `live-state-live` | — | `GET /api/v1/organization?filter[organization]=name==<org>` — confirms the PAT is valid AND the configured organisation is visible to it |

## Version compatibility

Tested against the v2.27 OpenAPI spec
([openapi-spec/v2_27_0.yml](https://github.com/terrakube-io/terrakube/blob/main/openapi-spec/v2_27_0.yml)).
Older versions that don't expose the workspace-jobs endpoint
(`/workspace/{wsId}/job`) degrade gracefully: the overlay logs a
`debug`-level line, returns a `LiveStateInfo` with workspace name + URL
but no run info, and skips the stale-apply check. The static inventory
on the rendered page is unaffected.

## Limitations vs the TFC overlay

* **No drift detection** — Terrakube doesn't expose a workspace-level
  drift attribute. If a future Terrakube release adds one,
  `TerrakubeOverlay._drift_status_from_workspace` is the single
  integration point.
* **No live resource count** — there is no equivalent of TFC's
  `/workspaces/{id}/resources` total-count endpoint. The state-history
  endpoint exists but parsing snapshots to count resources is non-trivial
  and version-dependent; we'd rather omit the field than fabricate it.

Everything else — workspace mapping, stale-apply alerts, the
`LiveStateOverlay` protocol surface — is identical to the TFC overlay,
so a team switching state platforms doesn't need to relearn the
config.
