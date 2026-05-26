variable "resource_group_name" {
  type        = string
  description = "Existing resource group to deploy into. Module does NOT create the RG itself."
}

variable "location" {
  type        = string
  default     = "westeurope"
  description = "Azure region. Container Apps + Key Vault are available in all standard regions."
}

variable "name" {
  type        = string
  default     = "iac-cartographer"
  description = "Name applied to the Container Apps Job, Key Vault, and User Assigned Identity."
}

variable "image" {
  type        = string
  default     = "ghcr.io/vakaobr/iac-cartographer:v0.1.0"
  description = "Container image. Pin to a semver tag in production; verify with cosign before bumping."
}

variable "schedule" {
  type        = string
  default     = "0 6 * * 1"
  description = "Cron expression. Container Apps Jobs use the Kubernetes-style 5-field cron format."
}

variable "config_yaml" {
  type        = string
  description = "Full iac-cartographer config.yaml body. Stored in Key Vault and mounted into the Job as a secret-backed env var."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Resource tags merged onto every resource the module creates."
}

variable "cpu" {
  type        = number
  default     = 1.0
  description = "CPU cores per replica. Container Apps Jobs accept 0.25, 0.5, 0.75, 1.0, … 4.0 (Consumption plan)."
}

variable "memory" {
  type        = string
  default     = "2Gi"
  description = "Memory per replica. Must match a valid CPU-memory pairing (e.g. 1.0 cpu → 2Gi)."
}

variable "replica_timeout" {
  type        = number
  default     = 3600
  description = "Per-replica timeout in seconds. 1h is comfortable for ~100 repos."
}

variable "replica_retry_limit" {
  type        = number
  default     = 0
  description = "Replica retry count on failure. 0 = no auto-retry, matches the k8s + GCP examples. Set to 1-2 if you have transient upstream-API flakiness."
}

variable "tenant_id" {
  type        = string
  description = "Azure AD tenant ID. Used for Key Vault access policies / RBAC scoping."
}
