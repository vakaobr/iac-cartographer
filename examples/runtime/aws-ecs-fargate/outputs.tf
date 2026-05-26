output "cluster_arn" {
  value       = aws_ecs_cluster.this.arn
  description = "ECS cluster ARN. Useful for cross-stack references."
}

output "task_definition_arn" {
  value       = aws_ecs_task_definition.this.arn
  description = "Latest task definition ARN. Bumps to a new revision on every image / config / env change."
}

output "schedule_arn" {
  value       = aws_scheduler_schedule.this.arn
  description = "EventBridge Scheduler ARN. Disable via `aws scheduler update-schedule --state DISABLED ...` during a maintenance window."
}

output "log_group_name" {
  value       = aws_cloudwatch_log_group.this.name
  description = "CloudWatch Logs group. Tail with `aws logs tail <name> --follow`."
}

output "config_parameter_name" {
  value       = aws_ssm_parameter.config.name
  description = "SSM Parameter Store path holding config.yaml. Update via `aws ssm put-parameter --overwrite --type SecureString --name <path> --value file://config.yaml`."
}

output "credential_secret_arns" {
  value       = { for k, v in aws_secretsmanager_secret.credentials : k => v.arn }
  description = "Logical-name → Secrets Manager ARN map. Populate each via `aws secretsmanager put-secret-value --secret-id <name> --secret-string '<json>'`."
}

output "task_role_arn" {
  value       = aws_iam_role.task.arn
  description = "Runtime IAM role iac-cartographer assumes inside the task. Grant additional permissions here (e.g. cross-account secrets reads) by attaching extra policies."
}

output "task_execution_role_arn" {
  value       = aws_iam_role.task_execution.arn
  description = "Task-execution role (image pulls + bootstrap secrets + log writes). Operators rarely need to extend this — extend the task role instead."
}

output "scheduler_role_arn" {
  value       = aws_iam_role.scheduler.arn
  description = "EventBridge Scheduler role (RunTask + PassRole only). Locked down — no operator-visible extension surface."
}
