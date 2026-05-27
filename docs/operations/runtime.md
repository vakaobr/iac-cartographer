# Running on a schedule

The CLI is a one-shot — `iac-cartographer --once` runs the whole
pipeline once and exits. Pair it with any scheduler:

| Scheduler | Setup | Best for |
|---|---|---|
| Kubernetes (Helm chart) | [`charts/iac-cartographer/`](https://github.com/vakaobr/iac-cartographer/tree/main/charts/iac-cartographer) | k8s clusters — recommended path. Templates the schedule, namespace, image tag, secrets backend, resources, workload-identity binding. |
| Kubernetes (raw manifest) | [`examples/runtime/kubernetes-cronjob.yaml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/kubernetes-cronjob.yaml) | Reading-and-copying reference for the raw shape. |
| AWS ECS Fargate + EventBridge Scheduler | [`examples/runtime/aws-ecs-fargate/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime/aws-ecs-fargate) (Terraform) | The reference AWS deployment — what the project was extracted from. Managed services, IAM identity, ~€1/month for a 50-repo weekly fleet. |
| GCP Cloud Run Jobs + Cloud Scheduler | [`examples/runtime/gcp-cloud-run-job/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime/gcp-cloud-run-job) (Terraform) | GCP-native batch path. Workload identity, per-second billing. |
| Azure Container Apps Jobs | [`examples/runtime/azure-container-apps-job/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime/azure-container-apps-job) (Terraform) | Azure-native batch path. AAD / Managed Identity wiring. |
| GitHub Actions | [`examples/runtime/github-actions.yml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/github-actions.yml) | Lightweight setup with no infrastructure to own; secrets via Actions secrets. |
| Plain cron / systemd-timer | [`examples/runtime/cron.sh`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/cron.sh) | A single VM you already own. Docker-based, no Python on the host. |

## Helm chart (recommended for k8s)

```bash
helm install my-iac-cartographer ./charts/iac-cartographer \
  --namespace iac-cartographer \
  --create-namespace \
  --values my-values.yaml
```

Minimal `my-values.yaml`:

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

For production, prefer `secrets.existingSecret: <name>` and manage the
Secret via External Secrets Operator / Sealed Secrets / SOPS. The chart
also exposes `serviceAccount.annotations` for IRSA / Workload Identity
/ Azure WI bindings.

See [`charts/iac-cartographer/README.md`](https://github.com/vakaobr/iac-cartographer/blob/main/charts/iac-cartographer/README.md)
for the full values reference.

## GitHub Actions

[`examples/runtime/github-actions.yml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/github-actions.yml) drops at
`.github/workflows/iac-cartographer.yml` in any repo (the iac-cartographer
fork itself, or any repo you want to host the schedule from).

Required Actions Secrets:

- `IAC_CARTOGRAPHER_SECRET_CONFLUENCE`
- `IAC_CARTOGRAPHER_SECRET_GITLAB`
- `IAC_CARTOGRAPHER_SECRET_GITHUB`
- `IAC_CARTOGRAPHER_SECRET_SLACK`
- `IAC_CARTOGRAPHER_SECRET_ANTHROPIC` (if `llm.backend: anthropic`)

Optional Variables (non-secret):

- `IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID`

Workflow locks `permissions: contents: read` and pulls the package
straight from PyPI (`pip install iac-cartographer`).

## Plain cron / systemd-timer

The bash wrapper at
[`examples/runtime/cron.sh`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/cron.sh)
drives the container image, so the host only needs Docker — no Python
install required. The same file includes an inline systemd-timer
alternative.

Install:

```bash
# Copy and make executable
sudo cp examples/runtime/cron.sh /usr/local/bin/iac-cartographer-run.sh
sudo chmod +x /usr/local/bin/iac-cartographer-run.sh

# Env file at /etc/iac-cartographer/env (mode 600):
#   IAC_CARTOGRAPHER_SECRET_CONFLUENCE={"email":"...","api_token":"..."}
#   IAC_CARTOGRAPHER_SECRET_GITLAB={"token":"..."}
#   ...
sudo install -m 600 /dev/stdin /etc/iac-cartographer/env <<'EOF'
IAC_CARTOGRAPHER_SECRET_CONFLUENCE={"email":"bot@x","api_token":"ATATT-..."}
EOF

# Config at /etc/iac-cartographer/config.yaml (see examples/config.example.yaml).
sudo cp config.yaml /etc/iac-cartographer/config.yaml

# Cron entry (root):
echo '0 6 * * 1 /usr/local/bin/iac-cartographer-run.sh >> /var/log/iac-cartographer.log 2>&1' \
  | sudo tee -a /etc/cron.d/iac-cartographer
```

## ECS Fargate + EventBridge

The original deployment iac-cartographer was extracted from uses ECS
Fargate + EventBridge Scheduler — the reference AWS path. A complete
Terraform module ships at
[`examples/runtime/aws-ecs-fargate/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime/aws-ecs-fargate)
(copy the example tfvars, edit, `terraform apply`). GCP Cloud Run Jobs
and Azure Container Apps Jobs modules ship alongside it under
[`examples/runtime/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime).
