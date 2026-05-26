variable "project_id" {
  type        = string
  description = "GCP project ID to deploy into."
}

variable "region" {
  type        = string
  default     = "europe-west1"
  description = "GCP region for the Cloud Run Job + Cloud Scheduler. Pick a region that has both services available."
}

variable "image" {
  type        = string
  default     = "ghcr.io/vakaobr/iac-cartographer:v0.1.0"
  description = "Container image. Pin to a semver tag in production; verify with cosign before bumping."
}

variable "name" {
  type        = string
  default     = "iac-cartographer"
  description = "Name applied to the Cloud Run Job, Cloud Scheduler, and the dedicated service account."
}

variable "schedule" {
  type        = string
  default     = "0 6 * * 1"
  description = "Cron expression for Cloud Scheduler. Defaults to 06:00 every Monday in the schedule_timezone."
}

variable "schedule_timezone" {
  type        = string
  default     = "UTC"
  description = "IANA timezone for the Cloud Scheduler cron. e.g. 'Europe/Berlin'."
}

variable "config_yaml" {
  type        = string
  description = "Full iac-cartographer config.yaml body. Mounted into the Job at /etc/iac-cartographer/config.yaml via a config-as-Secret entry."
}

variable "labels" {
  type        = map(string)
  default     = {}
  description = "Resource labels merged onto every resource the module creates."
}

# CPU + memory sized for ~50 repos. Cloud Run Jobs charge per
# vCPU-second + GiB-second the job is running, so over-allocation is
# wasteful — but under-allocation slows wall-clock time on terraform-docs.
variable "cpu" {
  type        = string
  default     = "1"
  description = "CPU allocation. Cloud Run Jobs accept '1', '2', '4', '6', '8'. Bump for fleets > 100 repos."
}

variable "memory" {
  type        = string
  default     = "1Gi"
  description = "Memory allocation. Bump for monorepos with deep .tf trees."
}

variable "task_timeout" {
  type        = string
  default     = "3600s"
  description = "Per-task timeout. 1h is comfortable for ~100 repos; bump for larger fleets."
}

variable "max_retries" {
  type        = number
  default     = 0
  description = "Cloud Run Job retries on task failure. 0 = no auto-retry, matches the k8s example's backoffLimit. Set to 1-2 if you have transient upstream-API flakiness."
}
