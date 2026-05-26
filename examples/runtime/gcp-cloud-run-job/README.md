# GCP — Cloud Run Jobs

Terraform module deploying iac-cartographer on GCP as a Cloud Run Job
triggered by Cloud Scheduler. Suits any GCP project that already uses
Cloud Run; charges per vCPU-second + GiB-second while the job runs.

## Layout

```
.
├── versions.tf                # Terraform + provider pins
├── variables.tf               # project_id, region, schedule, …
├── main.tf                    # the actual resources (Job, Scheduler, SA, Secret Manager)
├── outputs.tf                 # job_name, credential_secret_ids, …
├── terraform.tfvars.example   # copy + edit before apply
└── README.md                  # this file
```

Resources created:

- `google_cloud_run_v2_job.this` — the one-shot batch job.
- `google_cloud_scheduler_job.this` — cron trigger that POSTs to the
  Cloud Run admin API.
- `google_service_account.runner` — dedicated identity for both the Job
  and the Scheduler.
- `google_secret_manager_secret.config` — `config.yaml` body, mounted
  into the Job as a file at `/etc/iac-cartographer/config.yaml`.
- `google_secret_manager_secret.credentials` (5 entries) — credential
  bundles, mounted into the Job as `IAC_CARTOGRAPHER_SECRET_*` env vars.
- IAM bindings so the SA can read each secret.

## Apply

```bash
cd examples/runtime/gcp-cloud-run-job

cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars              # at minimum: project_id + config_yaml

terraform init
terraform plan
terraform apply
```

The apply succeeds with the credentials still empty — Secret Manager
secrets are created without versions. You can plan, apply, and inspect
the resources without leaking real tokens through Terraform state.

## Populate the credentials (one-off, after apply)

The credential secrets are created empty. The Cloud Run Job will fail
to start until each has a version. Add them via `gcloud`:

```bash
gcloud secrets versions add iac-cartographer-confluence \
  --data-file=<(echo -n '{"email":"bot@x","api_token":"ATATT..."}')

gcloud secrets versions add iac-cartographer-gitlab \
  --data-file=<(echo -n '{"token":"glpat-..."}')

gcloud secrets versions add iac-cartographer-github \
  --data-file=<(echo -n '{"token":"ghp_..."}')

gcloud secrets versions add iac-cartographer-slack \
  --data-file=<(echo -n '{"bot_token":"xoxb-..."}')

gcloud secrets versions add iac-cartographer-anthropic \
  --data-file=<(echo -n '{"api_key":"sk-ant-..."}')
```

Sourcing each secret from a different place is also fine — e.g. you
could populate `iac-cartographer-confluence` via External Secrets,
`iac-cartographer-anthropic` via SOPS-decrypted file, etc. The Job
references whichever version is `:latest` per secret.

## Trigger a one-shot run

```bash
gcloud run jobs execute iac-cartographer \
  --region europe-west1 \
  --wait
```

`--wait` blocks until the execution finishes and tails its logs. Useful
for the first run after `apply` to confirm everything's wired up.

## Inspect logs

```bash
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="iac-cartographer"' \
  --limit 100 --format json
```

Or via the Cloud Console: *Cloud Run → Jobs → iac-cartographer → Logs*.

## Roll out a config change

Update the `config_yaml` variable, then `terraform apply` — Terraform
notices the Secret Manager version drift and adds a new version. The
next Job execution picks up the new config automatically (the Job
references `version: "latest"`).

For a faster roll without a Terraform apply:

```bash
gcloud secrets versions add iac-cartographer-config \
  --data-file=path/to/new-config.yaml
```

The next scheduled run uses the new version.

## Why this shape

- **Cloud Run Jobs vs Cloud Run Services.** Services are
  request-driven and stay warm; Jobs are batch-driven and exit. The CLI
  is `iac-cartographer --once`, which exits — Jobs is the right
  primitive.
- **Cloud Scheduler vs cron-on-a-VM.** Same cost (a few cents/month);
  Scheduler is managed, retried automatically by GCP, and emits
  Cloud Logging entries for every fire. No instance to maintain.
- **Secret Manager vs env vars in the Job spec.** Job specs end up in
  Terraform state, in Cloud Logging audit logs, and in
  `gcloud run jobs describe` output. Secret Manager keeps the actual
  values out of all three.

## Cost

Typical run for ~50 repos:

- Cloud Run Job: ~5 minutes × 1 vCPU × 1 GiB ≈ **$0.005**.
- Cloud Scheduler: **$0.10/month** (first 3 jobs free, this is the 1st).
- Secret Manager: **$0.06/month** for 6 secrets (the first 6 active
  versions are free; you only pay for storage).
- Cloud Logging: bundled into the free tier for typical volumes.

Weekly runs ≈ **$0.02/month** in compute. The LLM call (Anthropic) is
typically the dominant cost; see the README's "Why → Cheap" line for
the order of magnitude.

## Switching LLM to Vertex AI

The Anthropic backend is the default in this example because the
Vertex AI iac-cartographer LLM backend is still on the
[roadmap](https://github.com/vakaobr/iac-cartographer#coming-next).
Once shipped, swap `llm.backend: anthropic` for `llm.backend: vertex`
in `config_yaml`. The SA will need
`roles/aiplatform.user` on the project.

For now, if you want everything on GCP, you can route through Vertex
AI's "Anthropic Claude on Vertex AI" endpoint via the Anthropic SDK by
overriding `anthropic_base_url` — but that's a Day 2 customisation
beyond this example.
