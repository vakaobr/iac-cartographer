# Runtime snippets

Drop-in deployment scaffolding for the schedulers iac-cartographer is
known to work well under. Each file is self-contained and annotated with
the parts you need to change.

| File | Scheduler | When to use |
|---|---|---|
| [`kubernetes-cronjob.yaml`](kubernetes-cronjob.yaml) | Kubernetes `CronJob` (raw) | Read-and-copy reference for the raw shape. For real deployments prefer the [Helm chart](../../charts/iac-cartographer/) — same resources but templated for schedule, namespace, secrets backend, workload identity, etc. |
| [`github-actions.yml`](github-actions.yml) | GitHub Actions `schedule` | Lightweight setup with no infrastructure to own; secrets live in the GitHub repo settings. |
| [`cron.sh`](cron.sh) | Plain `cron` / `systemd-timer` | A single VM you already own. Docker-based, so no Python install needed on the host. |

All three examples target the `env` secrets backend (no AWS account required). For the AWS / Vault backends, swap the secret source in each manifest accordingly — the application's command-line stays the same.

For the ECS Fargate + EventBridge deployment path the iac-cartographer
project was extracted from, see the (eventual) Terraform module in
[the roadmap](../../README.md#roadmap). The container image in
[`../../Dockerfile`](../../Dockerfile) is the canonical artifact for any
of these schedulers.
