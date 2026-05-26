# Cloud Run Job deployment for iac-cartographer on GCP.
#
# Shape:
#   * google_cloud_run_v2_job            — the one-shot batch job (image, args, env, resources).
#   * google_cloud_scheduler_job         — cron trigger that invokes the Cloud Run Job via its admin API.
#   * google_service_account             — dedicated identity for both the Run Job and Scheduler.
#   * google_secret_manager_secret + version — the IAC_CARTOGRAPHER_SECRET_* credential bundles.
#   * google_secret_manager_secret_iam_member — grants the SA read access to each secret.
#
# The Job is configured against the `env` iac-cartographer secrets backend
# — Cloud Run Jobs mount Secret Manager values as env vars natively, which
# maps perfectly onto IAC_CARTOGRAPHER_SECRET_<NAME>. No code change needed
# in iac-cartographer itself.
#
# After `terraform apply`, you still need to populate the secrets:
#   gcloud secrets versions add iac-cartographer-confluence \
#     --data-file=<(echo -n '{"email":"bot@x","api_token":"ATATT..."}')
# See README.md in this directory for the full list.

locals {
  base_labels = merge(
    {
      app        = var.name
      managed-by = "terraform"
    },
    var.labels,
  )

  # Logical secret names → Secret Manager IDs. The IDs are the operator-
  # visible names (`gcloud secrets list`); the env var names below map them
  # into the iac-cartographer env secrets backend mangling rules.
  secrets = {
    confluence = "${var.name}-confluence"
    gitlab     = "${var.name}-gitlab"
    github     = "${var.name}-github"
    slack      = "${var.name}-slack"
    anthropic  = "${var.name}-anthropic"
  }
}

# ─── Identity ──────────────────────────────────────────────────────────

resource "google_service_account" "runner" {
  project      = var.project_id
  account_id   = var.name
  display_name = "iac-cartographer Cloud Run Job runner"
  description  = "Reads secrets, executes terraform-docs + LLM calls + Confluence publish."
}

# Scheduler needs invoker permission on the Cloud Run Job to trigger runs.
resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = google_cloud_run_v2_job.this.project
  location = google_cloud_run_v2_job.this.location
  name     = google_cloud_run_v2_job.this.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runner.email}"
}

# ─── Config (mounted as a file) ────────────────────────────────────────

# Cloud Run Jobs don't support inline file mounts the way k8s ConfigMaps
# do, so the config.yaml lives in Secret Manager too. It's not secret per
# se but the storage shape (versioned, IAM-controlled, mountable as a
# file or env var) matches what we want.
resource "google_secret_manager_secret" "config" {
  project   = var.project_id
  secret_id = "${var.name}-config"
  labels    = local.base_labels

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "config" {
  secret      = google_secret_manager_secret.config.id
  secret_data = var.config_yaml
}

resource "google_secret_manager_secret_iam_member" "config_reader" {
  project   = google_secret_manager_secret.config.project
  secret_id = google_secret_manager_secret.config.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runner.email}"
}

# ─── Credential secrets (created empty; operator populates after apply) ─

resource "google_secret_manager_secret" "credentials" {
  for_each = local.secrets

  project   = var.project_id
  secret_id = each.value
  labels    = local.base_labels

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "credentials_reader" {
  for_each = google_secret_manager_secret.credentials

  project   = each.value.project
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runner.email}"
}

# ─── The Cloud Run Job ─────────────────────────────────────────────────

resource "google_cloud_run_v2_job" "this" {
  project  = var.project_id
  location = var.region
  name     = var.name
  labels   = local.base_labels

  # `Forbid` overlap — Cloud Run Jobs default to allowing concurrent
  # executions, but iac-cartographer is idempotent-per-content and
  # concurrent runs just waste LLM spend. The Scheduler is set to
  # `RETRY_AFTER_SOME_TIME` below so a still-running execution won't
  # be re-triggered.
  start_execution_token = ""

  template {
    task_count = 1
    parallelism = 1

    template {
      service_account = google_service_account.runner.email
      max_retries     = var.max_retries
      timeout         = var.task_timeout

      containers {
        image = var.image
        args  = ["--once", "--config", "/etc/iac-cartographer/config.yaml"]

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }

        # Mount the config Secret as a file. Cloud Run v2 supports
        # secret-volumes natively; the file lands at the configured
        # mount_path with `version: "latest"` semantics.
        volume_mounts {
          name       = "config"
          mount_path = "/etc/iac-cartographer"
        }

        # Mount each credential bundle as an env var. The env-secrets
        # backend reads IAC_CARTOGRAPHER_SECRET_<NAME> verbatim.
        dynamic "env" {
          for_each = local.secrets
          content {
            name = "IAC_CARTOGRAPHER_SECRET_${upper(env.key)}"
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.credentials[env.key].secret_id
                version = "latest"
              }
            }
          }
        }
      }

      volumes {
        name = "config"
        secret {
          secret = google_secret_manager_secret.config.secret_id
          items {
            version = "latest"
            path    = "config.yaml"
          }
        }
      }
    }
  }

  # The Cloud Run Job needs the credentials' IAM bindings before it can
  # start; depend_on makes that explicit (the env env-from refs don't
  # imply IAM access).
  depends_on = [
    google_secret_manager_secret_iam_member.credentials_reader,
    google_secret_manager_secret_iam_member.config_reader,
  ]
}

# ─── Scheduler ──────────────────────────────────────────────────────────

resource "google_cloud_scheduler_job" "this" {
  project   = var.project_id
  region    = var.region
  name      = var.name
  schedule  = var.schedule
  time_zone = var.schedule_timezone

  # Don't pile up triggers when a previous run is still in flight.
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.this.name}:run"

    oauth_token {
      service_account_email = google_service_account.runner.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}
