# ECS Fargate + EventBridge Scheduler deployment for iac-cartographer.
#
# Shape:
#   * aws_ecs_cluster                — minimal cluster (Fargate-only).
#   * aws_ecs_task_definition        — one-shot task, pulls image, mounts
#                                      config.yaml from SSM, exposes
#                                      Secrets Manager entries via the
#                                      task definition `secrets` block.
#   * aws_scheduler_schedule         — EventBridge Scheduler cron that
#                                      RunTask's the definition on the
#                                      configured cadence.
#   * aws_iam_role.task_execution    — pulls image + reads secrets + writes logs.
#   * aws_iam_role.task              — the runtime role iac-cartographer
#                                      itself uses (Bedrock invoke, SSM
#                                      parameter reads for the config).
#   * aws_iam_role.scheduler         — what EventBridge Scheduler assumes
#                                      to call ecs:RunTask.
#   * aws_secretsmanager_secret      — credential bundles, created empty
#                                      and populated by the operator
#                                      after apply.
#   * aws_ssm_parameter.config       — SecureString holding config.yaml
#                                      (mounted via environment_files).
#   * aws_cloudwatch_log_group       — task logs.
#
# This mirrors the actual production deployment the project was
# extracted from. The original ran the AWS-secrets backend (config in
# SSM, credentials in Secrets Manager) — same shape here.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  prefix = var.secrets_prefix != "" ? var.secrets_prefix : var.name

  # Logical secret names → Secrets Manager IDs. The IDs are the
  # operator-visible names (`aws secretsmanager list-secrets`); they
  # match the `iac-cartographer/<name>` convention the AWS secrets
  # backend uses when looking up `iac-cartographer/confluence` etc.
  base_secrets = toset([
    "confluence",
    "gitlab",
    "github",
    "slack",
  ])
  all_secret_names = setunion(local.base_secrets, var.extra_secrets)
  secret_ids = {
    for name in local.all_secret_names :
    name => "${local.prefix}/${name}"
  }

  tags = merge(
    {
      app        = var.name
      managed-by = "terraform"
    },
    var.tags,
  )
}

# ─── Logs ──────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/ecs/${var.name}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

# ─── Config (SSM Parameter Store, SecureString) ─────────────────────

# The iac-cartographer CLI reads `--config /etc/iac-cartographer/config.yaml`
# as a normal file. ECS Fargate doesn't support mounting SSM parameters
# as files natively (that's a 2023 capability for EC2-launch-type only),
# so we mount the config via task-definition `environment_files` — but
# that points at S3, not SSM. The cleanest path: copy the YAML into a
# SecureString SSM parameter and have the CLI's secrets backend
# (`secrets.backend: aws`) read it as a parameter at startup. The
# operator's config.yaml then has a single field set:
#
#   _bootstrap:
#     config_parameter_path: /iac-cartographer/config
#
# Actually no — easier path used here: the SSM parameter IS the config
# source. The container starts with no `--config` flag; the CLI's
# default config source is `ssm:///iac-cartographer/config`, exactly
# what this module creates. Zero local file involved.
resource "aws_ssm_parameter" "config" {
  name        = "/${var.name}/config"
  description = "iac-cartographer config.yaml body, consumed at task startup."
  type        = "SecureString"
  value       = var.config_yaml
  tags        = local.tags

  # The plaintext value is shown in `terraform plan` output unless you
  # use `terraform plan -no-color | less`. Reasonable trade-off: the
  # config.yaml isn't typically secret (no tokens) — that's what
  # Secrets Manager below is for.
}

# ─── Credential secrets (empty; operator populates after apply) ────────

resource "aws_secretsmanager_secret" "credentials" {
  for_each = local.secret_ids

  name        = each.value
  description = "iac-cartographer logical secret: ${each.key}"
  tags        = local.tags

  # Operator populates the secret via `aws secretsmanager put-secret-value`
  # after apply. Module-side defaults would either bake placeholder
  # values into TF state or rely on `ignore_changes`, both worse than
  # an explicit operator step.
}

# ─── IAM ───────────────────────────────────────────────────────────────

# Task execution role — pulls image + reads task-definition `secrets` +
# writes CW Logs. Distinct from the task role below; AWS keeps these
# separate so the container runtime CAN read the bootstrap secrets but
# the application itself runs with a NARROWER role.
resource "aws_iam_role" "task_execution" {
  name               = "${var.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The execution role needs explicit read access to the secrets that
# back the task definition's `secrets` block — the managed policy
# covers ECR + CW Logs but not Secrets Manager / SSM.
data "aws_iam_policy_document" "task_execution_extras" {
  statement {
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [for s in aws_secretsmanager_secret.credentials : s.arn]
  }
  statement {
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = [aws_ssm_parameter.config.arn]
  }
}

resource "aws_iam_role_policy" "task_execution_extras" {
  name   = "${var.name}-task-execution-extras"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.task_execution_extras.json
}

# Task role — what iac-cartographer itself runs as inside the
# container. Distinct from the execution role above: this role needs
# Bedrock invoke + Secrets Manager reads (the env-secrets backend uses
# `aws secretsmanager get-secret-value` from inside the task) + SSM
# read for the config parameter.
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "task" {
  # Bedrock InvokeModel for the default Claude inference profile. The
  # cross-region profile ARNs are EU-only here; mirror for other
  # regions when bumping the default model.
  statement {
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # `*` is intentionally broad — operators narrow it after picking
    # a specific inference profile and Claude variant. The default
    # config.yaml points at `eu.anthropic.claude-sonnet-4-5-...` so
    # the relevant ARNs are
    #   arn:aws:bedrock:eu-central-1:<acct>:inference-profile/eu.anthropic.claude-sonnet-4-5-*
    #   arn:aws:bedrock:eu-*::foundation-model/anthropic.claude-sonnet-4-5-*
    # See ARCHITECTURE / the LEARNINGS file for why both ARNs are needed.
    resources = ["*"]
  }

  # Read the credential secrets (the env-secrets backend AND the AWS
  # secrets backend both call get-secret-value at startup).
  statement {
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [for s in aws_secretsmanager_secret.credentials : s.arn]
  }

  # Read the config parameter.
  statement {
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.config.arn]
  }

  # CW Logs write (the task already writes via the execution role's
  # log driver, but the runtime also emits structured logs to the
  # same group — keep both paths working).
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.name}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# Scheduler role — what EventBridge Scheduler assumes when firing the
# cron. Distinct from the task / execution roles; the scheduler ONLY
# needs `ecs:RunTask` + `iam:PassRole` (to hand the execution + task
# roles to the new task).
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Scope the trust to this account to avoid the EventBridge
    # confused-deputy class of issue.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    effect    = "Allow"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.this.arn, "/:\\d+$/", ":*")]
    condition {
      test     = "ArnLike"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.this.arn]
    }
  }
  statement {
    effect = "Allow"
    actions = [
      "iam:PassRole",
    ]
    resources = [
      aws_iam_role.task_execution.arn,
      aws_iam_role.task.arn,
    ]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${var.name}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

# ─── Networking (optional default SG) ──────────────────────────────────

# When the caller doesn't pass a security_group_ids list, we create
# one with egress-all + no ingress. Fargate tasks don't accept
# inbound, so this is the right default.
resource "aws_security_group" "default" {
  count = length(var.security_group_ids) == 0 ? 1 : 0

  name        = "${var.name}-task"
  description = "iac-cartographer Fargate task — egress-all, no ingress"
  # vpc_id derived from the first subnet; assumes all subnets share a VPC.
  vpc_id = data.aws_subnet.first[0].vpc_id

  egress {
    description      = "All outbound to upstream APIs + image pulls"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = local.tags
}

data "aws_subnet" "first" {
  count = length(var.security_group_ids) == 0 ? 1 : 0
  id    = var.subnet_ids[0]
}

# ─── ECS cluster + task definition ────────────────────────────────────

resource "aws_ecs_cluster" "this" {
  name = var.name
  tags = local.tags

  setting {
    name  = "containerInsights"
    value = "disabled" # opt-in; turn on by overriding this in your fork
  }
}

resource "aws_ecs_task_definition" "this" {
  family                   = var.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn
  tags                     = local.tags

  container_definitions = jsonencode([
    {
      name      = var.name
      image     = var.image
      essential = true

      # The CLI's default config source is `ssm:///iac-cartographer/config`
      # — matches the parameter created above with no `--config` flag.
      # `IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID` would land here as
      # well via the `secrets` block below if you choose the env
      # secrets backend.
      command = ["--once"]

      environment = [
        {
          # If you're using the `env` secrets backend instead of the
          # default `aws` backend, set IAC_CARTOGRAPHER_SECRET_* via
          # the `secrets` block below — keeping this env array empty
          # is the right default.
          name  = "TZ"
          value = "UTC"
        },
      ]

      # ECS `secrets` injects Secrets Manager values as env vars on
      # container start. When the operator's config.yaml uses
      # `secrets.backend: env`, the names below match the
      # `IAC_CARTOGRAPHER_SECRET_<NAME>` convention. When using the
      # default `secrets.backend: aws`, this block is unused (the CLI
      # calls Secrets Manager itself with the task role).
      secrets = [
        for name, id in local.secret_ids :
        {
          name      = "IAC_CARTOGRAPHER_SECRET_${upper(name)}"
          valueFrom = aws_secretsmanager_secret.credentials[name].arn
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.this.name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])

  # Ensure the secrets exist before the task definition tries to
  # reference them (the arn lookup is on the resource attribute,
  # but the implicit dep graph misses the policy-attachment cycle
  # in some terraform versions; pin it explicitly).
  depends_on = [
    aws_iam_role_policy_attachment.task_execution_managed,
    aws_iam_role_policy.task_execution_extras,
    aws_iam_role_policy.task,
  ]
}

# ─── Scheduler ──────────────────────────────────────────────────────────

resource "aws_scheduler_schedule" "this" {
  name        = var.name
  description = "iac-cartographer scheduled run"
  group_name  = "default"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.schedule
  schedule_expression_timezone = var.schedule_timezone

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.this.arn
      task_count          = 1
      launch_type         = "FARGATE"
      platform_version    = "LATEST"

      network_configuration {
        subnets          = var.subnet_ids
        security_groups  = length(var.security_group_ids) > 0 ? var.security_group_ids : [aws_security_group.default[0].id]
        assign_public_ip = var.assign_public_ip ? "ENABLED" : "DISABLED"
      }
    }

    retry_policy {
      # Don't pile up triggers when a previous run is still in flight
      # or the API is down. iac-cartographer is idempotent-per-content,
      # so a missed run just means slightly stale pages until the next
      # scheduled invocation.
      maximum_event_age_in_seconds = var.task_timeout_seconds
      maximum_retry_attempts       = 0
    }
  }
}
