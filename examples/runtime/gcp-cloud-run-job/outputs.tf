output "job_name" {
  value       = google_cloud_run_v2_job.this.name
  description = "Cloud Run Job name; pass to `gcloud run jobs execute` for an ad-hoc run."
}

output "job_location" {
  value       = google_cloud_run_v2_job.this.location
  description = "Region the Cloud Run Job lives in."
}

output "service_account_email" {
  value       = google_service_account.runner.email
  description = "Identity used by both the Cloud Run Job and the Scheduler."
}

output "credential_secret_ids" {
  value       = { for k, v in google_secret_manager_secret.credentials : k => v.secret_id }
  description = "Map of logical credential names → Secret Manager IDs. After apply, populate each with `gcloud secrets versions add`."
}

output "config_secret_id" {
  value       = google_secret_manager_secret.config.secret_id
  description = "Secret Manager ID holding the rendered config.yaml. Update with `gcloud secrets versions add` to roll out a config change."
}
