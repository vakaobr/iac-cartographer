# Examples

End-to-end walkthroughs for the deployment targets iac-cartographer is
known to work well under. Each page sets out the runnable code, the
commands to drive it, and the operational knobs you'll usually want to
turn for production.

| Walkthrough | Best for | Runnable code |
|---|---|---|
| [docker-compose](docker-compose.md) | Local dev, on-prem VMs, air-gapped boxes. No cluster, no cloud. | [`examples/runtime/docker-compose/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime/docker-compose) |
| [Kubernetes (Helm chart)](kubernetes-helm.md) | k8s clusters with workload identity. Recommended k8s path. | [`charts/iac-cartographer/`](https://github.com/vakaobr/iac-cartographer/tree/main/charts/iac-cartographer) |
| [Kubernetes (raw manifest)](kubernetes-manifest.md) | Reading-and-copying reference for clusters without Helm. | [`examples/runtime/kubernetes-cronjob.yaml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/kubernetes-cronjob.yaml) |
| [GitHub Actions](github-actions.md) | Lightweight setup; no infrastructure to own. | [`examples/runtime/github-actions.yml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/github-actions.yml) |
| [Plain cron / systemd-timer](cron.md) | A single VM. Docker-based, no Python on the host. | [`examples/runtime/cron.sh`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/cron.sh) |

## Cloud-specific deployments

Terraform modules for the three major clouds — all live as
runnable code with READMEs covering the apply flow, secret seeding,
manual trigger commands, and customisation hooks (extra IAM, VPC
endpoints, etc.):

| Cloud | Module | Highlights |
|---|---|---|
| **AWS** | [`examples/runtime/aws-ecs-fargate/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime/aws-ecs-fargate) | ECS Fargate + EventBridge Scheduler. The reference deployment — what the project was extracted from. Managed services, IAM identity, ~€1/month for a 50-repo weekly fleet. |
| **GCP** | [`examples/runtime/gcp-cloud-run-job/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime/gcp-cloud-run-job) | Cloud Run Jobs + Cloud Scheduler. Workload-identity for both the Job and the Scheduler. Per-second billing. |
| **Azure** | [`examples/runtime/azure-container-apps-job/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime/azure-container-apps-job) | Container Apps Jobs. AAD / Managed Identity wiring; Key Vault for secrets when the `aws` backend isn't applicable. |

Each module follows the same 6-file shape: `versions.tf` (provider
pins), `variables.tf`, `main.tf`, `outputs.tf`,
`terraform.tfvars.example`, `README.md`. Copy the example tfvars,
edit, `terraform apply`.

## Picking the right one

Most adopters land on one of three:

- **Want it on k8s?** Use the [Helm chart](kubernetes-helm.md).
- **Don't have k8s?** Use [docker-compose](docker-compose.md) on whatever
  VM you've got.
- **Want zero infrastructure?** Use [GitHub Actions](github-actions.md) —
  secrets via Actions secrets, schedule in the workflow file.

The pluggable backends ([publishers](../backends/publishers.md),
[LLM](../backends/llm.md), [secrets](../backends/secrets.md),
[discovery](../backends/discovery.md)) are orthogonal to deployment
target — pick the runtime, then wire up backends independently.
