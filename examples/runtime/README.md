# Runtime examples

Drop-in deployment scaffolding for the schedulers iac-cartographer is
known to work well under. Each is self-contained and annotated with the
parts you need to change.

## Single-file snippets

| File | Scheduler | When to use |
|---|---|---|
| [`kubernetes-cronjob.yaml`](kubernetes-cronjob.yaml) | Kubernetes `CronJob` (raw manifest) | Read-and-copy reference for the raw shape. For real deployments prefer the [Helm chart](../../charts/iac-cartographer/) — same resources but templated for schedule, namespace, secrets backend, workload identity, etc. |
| [`github-actions.yml`](github-actions.yml) | GitHub Actions `schedule` | Lightweight setup with no infrastructure to own; secrets live in the GitHub repo settings. See also the [marketplace action](#using-the-marketplace-action) below — same trigger, less YAML. |
| [`cron.sh`](cron.sh) | Plain `cron` / `systemd-timer` | A single VM you already own. Docker-based, so no Python install needed on the host. |

All three target the `env` secrets backend (no AWS account required). For
the AWS / Vault backends, swap the secret source in each manifest
accordingly — the application's command-line stays the same.

## Multi-file deployment recipes

Self-contained Terraform / compose modules for the three major clouds
plus one cloud-free local recipe. Each subdirectory ships its own
README with the apply / up / scale flow, secret seeding, manual trigger
commands, and customisation hooks (extra IAM, VPC endpoints, etc.).

| Directory | Stack | Highlights |
|---|---|---|
| [`docker-compose/`](docker-compose/) | docker-compose v2 | Local dev, on-prem VMs, air-gapped boxes. No cluster, no cloud. |
| [`aws-ecs-fargate/`](aws-ecs-fargate/) | AWS ECS Fargate + EventBridge Scheduler | The reference deployment — what the project was extracted from. Managed services, IAM identity, ~€1/month for a 50-repo weekly fleet. |
| [`gcp-cloud-run-job/`](gcp-cloud-run-job/) | GCP Cloud Run Jobs + Cloud Scheduler | Workload-identity for both the Job and the Scheduler. Per-second billing. |
| [`azure-container-apps-job/`](azure-container-apps-job/) | Azure Container Apps Jobs | AAD / Managed Identity wiring; Key Vault for secrets when the `aws` backend isn't applicable. |

Each Terraform module follows the same six-file shape: `versions.tf`
(provider pins), `variables.tf`, `main.tf`, `outputs.tf`,
`terraform.tfvars.example`, `README.md`. Copy the example tfvars, edit,
`terraform apply`.

## Using the marketplace action

The repo also publishes a reusable
[GitHub Action](https://github.com/marketplace/actions/iac-cartographer)
that wraps the container image — same logic as
[`github-actions.yml`](github-actions.yml), but with `uses:` instead of
a hand-rolled `docker run`. Shortest possible workflow:

```yaml
name: Refresh IaC inventory
on:
  schedule:
    - cron: "0 6 * * 1"   # Mondays at 06:00 UTC
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: vakaobr/iac-cartographer@v0.1.2
        with:
          config: ./iac-cartographer.config.yaml
        env:
          # Map secrets into env vars; matches the `env` secrets backend.
          IAC_CARTOGRAPHER_CONFLUENCE_API_TOKEN: ${{ secrets.CONFLUENCE_API_TOKEN }}
          IAC_CARTOGRAPHER_ANTHROPIC_API_KEY:    ${{ secrets.ANTHROPIC_API_KEY }}
          IAC_CARTOGRAPHER_GITHUB_TOKEN:         ${{ secrets.IAC_GITHUB_TOKEN }}
          IAC_CARTOGRAPHER_SLACK_BOT_TOKEN:      ${{ secrets.SLACK_BOT_TOKEN }}
```

The action's [`action.yml`](../../action.yml) defines the full set of
inputs (`dry-run`, `diff`, `model`, `repos`, `extra-args`, …). For the
verbose hand-rolled workflow with finer control — including pinning the
image by digest, using AWS / Vault backends, and uploading the produced
artefacts as workflow artefacts — see
[`github-action-marketplace.yml`](github-action-marketplace.yml) and
[`github-actions.yml`](github-actions.yml).

## Picking the right one

Most adopters land on one of three:

- **Want it on k8s?** Use the [Helm chart](../../charts/iac-cartographer/).
- **Don't have k8s?** Use [`docker-compose/`](docker-compose/) on whatever VM you've got.
- **Want zero infrastructure to own?** Use the [marketplace action](#using-the-marketplace-action) — secrets via Actions secrets, schedule in the workflow file.

The pluggable backends (publishers / LLM / secrets / discovery /
notifications) are orthogonal to the deployment target — pick the
runtime, then wire up backends independently. The canonical container
image lives at `ghcr.io/vakaobr/iac-cartographer:vX.Y.Z` (multi-arch:
`linux/amd64` + `linux/arm64`).
