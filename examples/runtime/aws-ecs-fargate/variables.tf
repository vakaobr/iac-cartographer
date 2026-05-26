variable "region" {
  type        = string
  default     = "eu-central-1"
  description = "AWS region for ECS Fargate + EventBridge Scheduler + Secrets Manager + CloudWatch Logs."
}

variable "name" {
  type        = string
  default     = "iac-cartographer"
  description = "Name applied to the ECS cluster, task definition, scheduler, IAM roles, and log group. Doubles as the Secrets Manager prefix when var.secrets_prefix is left blank."
}

variable "image" {
  type        = string
  default     = "ghcr.io/vakaobr/iac-cartographer:v0.1.0"
  description = "Container image. Pin to a semver tag in production; verify with cosign before bumping. Cross-region pulls from GHCR work but add ~3s of cold start — mirror to ECR Public for the lowest latency."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Subnet IDs the Fargate task runs in. MUST have egress to the internet (NAT or VPC endpoint) so the task can reach GitLab / GitHub / Confluence / Bedrock / Slack. Module does NOT create a VPC — bring your own."
}

variable "security_group_ids" {
  type        = list(string)
  default     = []
  description = "Optional list of security group IDs attached to the task ENI. When empty the module creates an outbound-all SG (no inbound rules — Fargate tasks accept no traffic)."
}

variable "assign_public_ip" {
  type        = bool
  default     = false
  description = "Whether the task ENI gets a public IP. Default false (most VPCs route egress via NAT). Set true ONLY for public subnets without a NAT."
}

variable "schedule" {
  type        = string
  default     = "cron(0 6 ? * MON *)"
  description = "EventBridge Scheduler expression. EventBridge uses `cron(MIN HOUR DAY-OF-MONTH MONTH DAY-OF-WEEK YEAR)` — note `?` for unused day-of-month + 6-field cron (different from `cron.sh`!). Defaults to Monday 06:00."
}

variable "schedule_timezone" {
  type        = string
  default     = "UTC"
  description = "IANA timezone for the schedule. e.g. 'Europe/Berlin'."
}

variable "config_yaml" {
  type        = string
  description = "Full iac-cartographer config.yaml body. Loaded via `--config /etc/iac-cartographer/config.yaml`; the module writes it into a SecureString SSM parameter and mounts it via task-definition environment_files. For very large configs (>4kb), set secrets_backend = \"aws\" inside the YAML and let SSM Parameter Store hold the values."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Resource tags merged onto every resource the module creates."
}

# Resource sizing. ECS Fargate charges per vCPU-hour + GB-hour the
# task is running. Default targets ~50 repos; bump for larger fleets.
variable "task_cpu" {
  type        = string
  default     = "512"
  description = "Fargate task CPU. Valid combinations: 256/512/1024/2048/4096. See AWS Fargate task definition reference."
}

variable "task_memory" {
  type        = string
  default     = "1024"
  description = "Fargate task memory in MB. Must pair with task_cpu — see AWS Fargate task definition reference."
}

variable "task_timeout_seconds" {
  type        = number
  default     = 3600
  description = "EventBridge Scheduler hard timeout for the ECS task. 1h is comfortable for ~100 repos. ECS itself has no per-task wall-clock limit — this enforces one."
}

variable "log_retention_days" {
  type        = number
  default     = 30
  description = "CloudWatch Logs retention. Per-month CW Logs is the dominant cost for chatty deployments — 30d covers debugging without growing forever."
}

# Secrets Manager wiring. The module CREATES empty secrets so the
# apply doesn't pull tokens through Terraform state. The operator
# populates them via the AWS CLI after `terraform apply` succeeds.
variable "secrets_prefix" {
  type        = string
  default     = ""
  description = "Secrets Manager name prefix. Defaults to var.name when blank — secrets land at `<prefix>/confluence`, `<prefix>/gitlab`, etc. Match the `iac-cartographer/<name>` convention in the iac-cartographer secrets backend when populating."
}

variable "extra_secrets" {
  type        = set(string)
  default     = []
  description = "Extra Secrets Manager entries to create beyond the always-required four (confluence, gitlab, github, slack). Add `anthropic` here if `llm.backend: anthropic`; `bitbucket` if Bitbucket discovery is enabled; pager / webhook / teams / etc. for notification channels."
}
