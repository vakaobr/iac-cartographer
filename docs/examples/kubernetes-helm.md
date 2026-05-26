# Kubernetes (Helm chart)

End-to-end walkthrough for deploying iac-cartographer on Kubernetes as
a templated CronJob via the bundled Helm chart.

**When to pick this:** any k8s cluster — this is the recommended k8s
path. The chart templates everything you'd usually customise (schedule,
namespace, image tag, secrets backend, resources, workload-identity
binding) and slots into ExternalSecrets / SealedSecrets / SOPS via
`secrets.existingSecret`.

The chart lives at
[`charts/iac-cartographer/`](https://github.com/vakaobr/iac-cartographer/tree/main/charts/iac-cartographer).

## Install

```bash
git clone https://github.com/vakaobr/iac-cartographer.git

helm install my-iac-cartographer ./iac-cartographer/charts/iac-cartographer \
  --namespace iac-cartographer \
  --create-namespace \
  --values my-values.yaml
```

## Minimal `my-values.yaml`

For getting started — credentials inline in Helm values. Not suitable
for production; see below.

```yaml
image:
  tag: v0.1.0

cronjob:
  schedule: "0 6 * * 1"

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

secrets:
  stringData:
    IAC_CARTOGRAPHER_SECRET_CONFLUENCE: '{"email":"bot@acme","api_token":"ATATT..."}'
    IAC_CARTOGRAPHER_SECRET_GITLAB:     '{"token":"glpat-..."}'
    IAC_CARTOGRAPHER_SECRET_GITHUB:     '{"token":"ghp_..."}'
    IAC_CARTOGRAPHER_SECRET_SLACK:      '{"bot_token":"xoxb-..."}'
    IAC_CARTOGRAPHER_SECRET_ANTHROPIC:  '{"api_key":"sk-ant-..."}'
```

## Production-shape `my-values.yaml`

Real credentials managed out-of-band; ServiceAccount wired to your
cloud's workload identity.

```yaml
image:
  tag: v0.1.0
  # pullSecrets: [{name: my-ghcr-pull-secret}]  # for private mirror

cronjob:
  schedule: "0 6 * * 1"

config:
  existingConfigMap: iac-cartographer-config  # managed elsewhere

secrets:
  existingSecret: iac-cartographer-secrets    # ExternalSecrets / SealedSecrets / SOPS

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

## Trigger a one-shot run

```bash
kubectl create job \
  --from=cronjob/my-iac-cartographer \
  iac-cartographer-test-$(date +%s) \
  --namespace iac-cartographer
```

Tail the logs:

```bash
kubectl logs \
  --namespace iac-cartographer \
  --selector app.kubernetes.io/instance=my-iac-cartographer \
  --tail=200 --follow
```

## Verify the rendered config

```bash
kubectl get configmap \
  --namespace iac-cartographer \
  my-iac-cartographer-config \
  -o jsonpath='{.data.config\.yaml}'
```

## Values reference

See [`values.yaml`](https://github.com/vakaobr/iac-cartographer/blob/main/charts/iac-cartographer/values.yaml)
for the full annotated default set. Highlights:

| Key | Default | What it does |
|---|---|---|
| `image.repository` | `ghcr.io/vakaobr/iac-cartographer` | Container image. |
| `image.tag` | chart `appVersion` | Pin to a semver tag in production. |
| `cronjob.schedule` | `0 6 * * 1` | Cron expression. |
| `cronjob.concurrencyPolicy` | `Forbid` | Don't overlap runs. |
| `cronjob.timeZone` | unset | Optional k8s 1.27+ override. |
| `config.appConfig` | minimal scaffold | Inline `config.yaml` body. Use `config.existingConfigMap` instead for GitOps-managed configs. |
| `secrets.stringData` | empty | Inline credentials. Use `secrets.existingSecret` for production. |
| `serviceAccount.annotations` | `{}` | IRSA / Workload Identity / Azure WI binding. |
| `resources.{requests,limits}` | 200m/512Mi … 1/1Gi | Sized for ~50 repos. |
| `extraArgs` | `[]` | Extra args appended to `iac-cartographer --once`. |
| `extraEnv` | `[]` | Extra env vars (e.g. `VAULT_TOKEN` from a sidecar Secret). |
