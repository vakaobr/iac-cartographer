# Kubernetes (raw manifest)

Single-file CronJob manifest at
[`examples/runtime/kubernetes-cronjob.yaml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/kubernetes-cronjob.yaml).

**When to pick this:** read-and-copy reference for learning the
deployment shape, or clusters where Helm isn't available. For most
production deployments, use the [Helm chart](kubernetes-helm.md)
instead — same resources, but templated for the parts adopters
actually customise.

## Layout

The manifest produces four resources:

- `Namespace iac-cartographer`
- `ConfigMap iac-cartographer-config` — the `config.yaml` body.
- `Secret iac-cartographer-secrets` — the `IAC_CARTOGRAPHER_SECRET_*`
  env vars (Opaque, `stringData`).
- `CronJob iac-cartographer` — `0 6 * * 1`, `concurrencyPolicy: Forbid`,
  non-root, all caps dropped, `backoffLimit: 0`.

## Apply

```bash
# Replace the placeholders in-line OR copy the file first and edit.
$EDITOR examples/runtime/kubernetes-cronjob.yaml
kubectl apply -f examples/runtime/kubernetes-cronjob.yaml
```

Adjustments you'll typically make before applying:

- **Image tag** — pin to `:v0.1.0` rather than `:latest` in production.
- **`cronjob.schedule`** — see [crontab.guru](https://crontab.guru/).
- **Secret payloads** — the placeholders are obviously fake (`ATATT-replace-me`).
- **`ConfigMap config.yaml`** — discovery scope, publisher choice, etc.

## Trigger a one-shot test run

```bash
kubectl create job \
  --from=cronjob/iac-cartographer \
  iac-cartographer-test-$(date +%s) \
  --namespace iac-cartographer
```

Tail the logs:

```bash
kubectl logs --namespace iac-cartographer \
  --selector job-name=iac-cartographer-test-... \
  --tail=200 --follow
```

## Differences from the Helm chart

| Aspect | Raw manifest | Helm chart |
|---|---|---|
| Customisation | Edit YAML in place | `--values` overlay |
| Secret management | Inline `stringData` only | `existingSecret` for ExternalSecrets / SealedSecrets / SOPS |
| Config management | Inline ConfigMap | `existingConfigMap` for GitOps-managed configs |
| Workload identity | Edit the SA's annotations by hand | `serviceAccount.annotations:` value |
| Multi-env | `kustomize` overlays needed | One values file per env |
| Upgrade | `kubectl apply` | `helm upgrade` (revision-tracked) |

If you find yourself templating the raw manifest with `sed` or `envsubst`
for more than two environments, it's time to switch to the Helm chart.
