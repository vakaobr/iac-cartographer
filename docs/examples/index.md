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

Coming up as separate walkthroughs (see the [roadmap on GitHub](https://github.com/vakaobr/iac-cartographer#coming-next)):

- **AWS** — ECS Fargate + EventBridge Scheduler (the path the project was extracted from). Pending the Terraform module.
- **GCP** — Cloud Run Jobs + Cloud Scheduler.
- **Azure** — Container Apps Jobs + Logic Apps schedule.

Each will live as its own page under `docs/examples/` with a matching
`examples/runtime/<cloud>/` directory for the runnable Terraform / gcloud /
az manifests.

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
