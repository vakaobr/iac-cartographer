# Runtime snippets

Drop-in deployment scaffolding for the schedulers iac-cartographer is
known to work well under. Each file is self-contained and annotated with
the parts you need to change.

| File | Scheduler | When to use |
|---|---|---|
| [`kubernetes-cronjob.yaml`](kubernetes-cronjob.yaml) | Kubernetes `CronJob` | k8s clusters with a workload identity solution (IRSA, Workload Identity, Pod Identity) or with the `env` secrets backend. |
| [`github-actions.yml`](github-actions.yml) | GitHub Actions `schedule` | Lightweight setup with no infrastructure to own; secrets live in the GitHub repo settings. |
| [`cron.sh`](cron.sh) | Plain `cron` / `systemd-timer` | A single VM you already own. Docker-based, so no Python install needed on the host. |

All three examples target the `env` secrets backend (no AWS account required). For the AWS / Vault backends, swap the secret source in each manifest accordingly — the application's command-line stays the same.

For the ECS Fargate + EventBridge deployment path the iac-cartographer
project was extracted from, see the (eventual) Terraform module in
[the roadmap](../../README.md#roadmap). The container image in
[`../../Dockerfile`](../../Dockerfile) is the canonical artifact for any
of these schedulers.
