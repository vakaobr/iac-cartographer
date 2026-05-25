# iac-cartographer Helm chart

A Kubernetes Helm chart that schedules
[iac-cartographer](https://github.com/vakaobr/iac-cartographer) as a CronJob.
Produces the same deployment shape as
[`examples/runtime/kubernetes-cronjob.yaml`](../../examples/runtime/kubernetes-cronjob.yaml)
but with proper templating for namespace, schedule, secrets backend,
resources, and workload identity bindings.

## Install

```bash
helm install my-iac-cartographer ./charts/iac-cartographer \
  --namespace iac-cartographer \
  --create-namespace \
  --values my-values.yaml
```

Once the chart is published to a registry (planned, see roadmap):

```bash
helm install my-iac-cartographer oci://ghcr.io/vakaobr/charts/iac-cartographer \
  --version 0.1.0 \
  --namespace iac-cartographer \
  --create-namespace \
  --values my-values.yaml
```

## Minimal `my-values.yaml`

```yaml
image:
  tag: v0.1.0

cronjob:
  schedule: "0 6 * * 1"

# Inline the iac-cartographer config. See examples/config.example.yaml
# in the parent repo for the full annotated reference.
config:
  appConfig:
    discovery:
      gitlab_group_ids: [15]
      github_orgs: ["acme-org"]
    secrets:
      backend: env
    llm:
      backend: anthropic
      model_id: claude-sonnet-4-5-20250929
    publisher:
      kind: confluence
    confluence:
      site: acme.atlassian.net
      space_key: DOCS
      parent_page_id: "123456789"
    slack:
      channel: "#alerts"

# Credentials. For production, prefer `existingSecret` pointing at an
# External Secrets / Sealed Secrets / SOPS-managed Secret.
secrets:
  stringData:
    IAC_CARTOGRAPHER_SECRET_CONFLUENCE: '{"email":"bot@acme","api_token":"ATATT..."}'
    IAC_CARTOGRAPHER_SECRET_GITLAB:     '{"token":"glpat-..."}'
    IAC_CARTOGRAPHER_SECRET_GITHUB:     '{"token":"ghp_..."}'
    IAC_CARTOGRAPHER_SECRET_SLACK:      '{"bot_token":"xoxb-..."}'
    IAC_CARTOGRAPHER_SECRET_ANTHROPIC:  '{"api_key":"sk-ant-..."}'
```

## Production-shape `my-values.yaml`

For real deployments, wire the Secret up to whatever's managing your
secrets out-of-band, and use the ServiceAccount annotations to bind to
your cloud workload identity:

```yaml
image:
  tag: v0.1.0
  # If pulling from a private registry:
  # pullSecrets: [{name: my-ghcr-pull-secret}]

cronjob:
  schedule: "0 6 * * 1"

config:
  existingConfigMap: iac-cartographer-config    # managed elsewhere

secrets:
  existingSecret: iac-cartographer-secrets      # ExternalSecrets / SealedSecrets / SOPS

serviceAccount:
  create: true
  annotations:
    # EKS / IRSA:
    eks.amazonaws.com/role-arn: arn:aws:iam::111122223333:role/iac-cartographer
    # GKE Workload Identity (alternative):
    # iam.gke.io/gcp-service-account: iac-cartographer@PROJECT.iam.gserviceaccount.com
    # Azure Workload Identity (alternative):
    # azure.workload.identity/client-id: 00000000-0000-0000-0000-000000000000

resources:
  requests:
    cpu: 200m
    memory: 512Mi
  limits:
    cpu: 1
    memory: 1Gi
```

## Values reference

See [`values.yaml`](values.yaml) for the full annotated default set.
Highlights:

| Key | Default | What it does |
|---|---|---|
| `image.repository` | `ghcr.io/vakaobr/iac-cartographer` | Container image. Override for a private mirror. |
| `image.tag` | chart `appVersion` | Pin to a semver tag in production. |
| `cronjob.schedule` | `0 6 * * 1` | Cron expression in the cluster's timezone. |
| `cronjob.concurrencyPolicy` | `Forbid` | Don't overlap runs. |
| `cronjob.timeZone` | unset | Optional k8s 1.27+ timezone override. |
| `config.appConfig` | minimal scaffold | Inline `config.yaml` body. Set `config.existingConfigMap` to reference an externally-managed ConfigMap instead. |
| `secrets.stringData` | empty | Inline credentials. Set `secrets.existingSecret` to point at an externally-managed Secret. |
| `serviceAccount.annotations` | `{}` | Drop your IRSA / Workload Identity / Azure WI binding here. |
| `resources.{requests,limits}` | 200m/512Mi … 1/1Gi | Sized for ~50 repos. |
| `extraArgs` | `[]` | Extra args appended to `iac-cartographer --once`. |
| `extraEnv` | `[]` | Extra env vars (e.g. `VAULT_TOKEN` from a sidecar-injected Secret). |

## Verify before applying

```bash
helm template my-iac-cartographer ./charts/iac-cartographer \
  --values my-values.yaml | kubectl apply --dry-run=client -f -

helm lint ./charts/iac-cartographer --values my-values.yaml
```

## After install

The post-install `NOTES.txt` prints the cron schedule, the image
reference, and commands to:

* Trigger an immediate one-shot test run (`kubectl create job --from=cronjob/...`).
* Tail logs from the pod.
* Inspect the resolved `config.yaml` in the ConfigMap.
