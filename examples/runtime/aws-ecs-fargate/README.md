# AWS — ECS Fargate + EventBridge Scheduler

Terraform module deploying iac-cartographer on AWS as an ECS Fargate
task triggered by **EventBridge Scheduler**. This mirrors the
production deployment the project was extracted from — same shape
the upstream operator runs in their own AWS account.

Charges: Fargate (per vCPU-hour + GB-hour, only while the task is
running), CloudWatch Logs (per GB ingested + retention), Secrets
Manager (per secret-month), SSM Parameter Store (free up to 10k
parameters; advanced tier per parameter-month). A typical 50-repo
weekly run costs **under €1/month** end-to-end.

## Layout

```
.
├── versions.tf                # Terraform + AWS provider pins
├── variables.tf               # region, subnets, schedule, …
├── main.tf                    # the actual resources (cluster, task, scheduler, IAM, secrets, logs)
├── outputs.tf                 # cluster ARN, log group, credential secret ARNs, …
├── terraform.tfvars.example   # copy + edit before apply
└── README.md                  # this file
```

Resources created:

- `aws_ecs_cluster.this` — minimal Fargate-only cluster.
- `aws_ecs_task_definition.this` — one-shot task referencing the
  configured image. CPU / memory tunable via variables.
- `aws_scheduler_schedule.this` — EventBridge Scheduler cron that
  `RunTask`s the task definition on the configured cadence.
- `aws_iam_role.task_execution` — ECR pull + Secrets Manager bootstrap + CW Logs.
- `aws_iam_role.task` — runtime role iac-cartographer assumes inside
  the container (Bedrock invoke + Secrets Manager + SSM).
- `aws_iam_role.scheduler` — EventBridge Scheduler's assume-role for
  `ecs:RunTask` + `iam:PassRole`. Scoped to this account to avoid the
  confused-deputy class of issue.
- `aws_secretsmanager_secret.credentials` — credential bundles
  (confluence + gitlab + github + slack always; opt-in extras via
  `extra_secrets`). Created **empty** — operator populates after apply.
- `aws_ssm_parameter.config` — SecureString holding `config.yaml`. The
  iac-cartographer CLI reads this via its default config source
  (`ssm:///iac-cartographer/config`).
- `aws_cloudwatch_log_group.this` — task logs with configurable retention.
- `aws_security_group.default` — outbound-all, no inbound. Only created
  when `security_group_ids` is empty.

## Pre-requisites

- An AWS account with the `aws` Terraform provider configured (env vars,
  profile, or workload identity from your CI runner).
- An existing VPC with **at least one subnet that has egress to the
  internet** — either:
  - Private subnets routed through a NAT gateway (the common shape), or
  - Public subnets with an Internet Gateway + `assign_public_ip = true`.
- The Bedrock model you reference in `config_yaml` must be enabled in
  your account: [Bedrock console → Model access].
- IAM permissions in the apply role to create roles, secrets, SSM
  parameters, ECS resources, EventBridge schedules, and CloudWatch
  log groups.

## Apply

```bash
cd examples/runtime/aws-ecs-fargate

cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars              # at minimum: subnet_ids + config_yaml

terraform init
terraform plan
terraform apply
```

The apply succeeds with the credentials still empty — Secrets Manager
entries are created without versions. You can plan, apply, and inspect
the resources without leaking real tokens through Terraform state.

## Populate the credentials (one-off, after apply)

The credential secrets are created empty. The Fargate task will fail to
start until each has a value. Populate them via the AWS CLI:

```bash
NAME=iac-cartographer  # matches var.name

aws secretsmanager put-secret-value \
  --secret-id $NAME/confluence \
  --secret-string '{"email":"bot@example.com","api_token":"ATATT..."}'

aws secretsmanager put-secret-value \
  --secret-id $NAME/gitlab \
  --secret-string '{"token":"glpat-..."}'

aws secretsmanager put-secret-value \
  --secret-id $NAME/github \
  --secret-string '{"token":"ghp_..."}'

aws secretsmanager put-secret-value \
  --secret-id $NAME/slack \
  --secret-string '{"bot_token":"xoxb-..."}'

# Add `extra_secrets` entries here too, e.g.:
# aws secretsmanager put-secret-value \
#   --secret-id $NAME/anthropic \
#   --secret-string '{"api_key":"sk-ant-..."}'
```

The logical-name → ARN map is in the `credential_secret_arns` output:

```bash
terraform output credential_secret_arns
```

## Trigger a manual run

EventBridge Scheduler fires on the configured cron; for an on-demand
run (after populating secrets, after a config change, before pushing
to a wider audience), invoke `ecs run-task` directly:

```bash
aws ecs run-task \
  --cluster $(terraform output -raw cluster_arn) \
  --task-definition $(terraform output -raw task_definition_arn) \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<your-subnet>],securityGroups=[<your-sg>],assignPublicIp=DISABLED}"
```

(`securityGroups` only needed when you supplied `security_group_ids`;
otherwise the module-created default SG is referenced by the scheduler
target and you can read its ID from `terraform state show`.)

Watch the logs:

```bash
aws logs tail $(terraform output -raw log_group_name) --follow
```

## Updating the config

The config lives in SSM Parameter Store, not in the task definition,
so a config change does NOT require a new task definition revision.
Update via:

```bash
aws ssm put-parameter --overwrite \
  --type SecureString \
  --name $(terraform output -raw config_parameter_name) \
  --value "$(< config.yaml)"
```

The next scheduled run picks up the new value automatically.

## Updating the image

Bumping the image (`var.image`) creates a new task definition revision;
the next scheduled run uses the new revision. Roll back by updating
the variable to the previous tag and re-applying.

For zero-downtime image updates with manual cosign verification:

```bash
# Verify the signature
cosign verify ghcr.io/vakaobr/iac-cartographer:v0.2.0 \
  --certificate-identity-regexp "https://github.com/vakaobr/iac-cartographer/.*" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com"

# Bump the variable
sed -i 's/v0.1.0/v0.2.0/' terraform.tfvars
terraform apply
```

## Disabling temporarily

To pause runs without destroying anything:

```bash
aws scheduler update-schedule \
  --name $(terraform output -raw schedule_arn | cut -d/ -f3-) \
  --state DISABLED
```

Re-enable with `--state ENABLED`. Cleaner than commenting out the
schedule in HCL and re-applying.

## Customising the IAM permissions

The `task_role_arn` output exposes the runtime role. Attach extra
policies for things this module doesn't cover by default:

- **Cross-account Secrets Manager reads** (split-stack deployments):
  ```hcl
  resource "aws_iam_role_policy_attachment" "cross_account_secrets" {
    role       = module.iac_cartographer.task_role_arn  # if using the module
    policy_arn = aws_iam_policy.cross_account_secrets_read.arn
  }
  ```

- **VPC endpoints for outbound traffic restrictions**: the default
  task role + SG don't need a VPC endpoint — outbound 443 to GitLab /
  GitHub / Confluence / Bedrock / Slack works through a NAT. For
  no-internet deployments, set up VPC endpoints for Bedrock + Secrets
  Manager + SSM + Logs + ECR, and tighten the SG egress to those
  endpoint CIDRs.

## Comparison with other runtimes

| Path | When to use |
|---|---|
| **This** (ECS Fargate + EventBridge) | You're on AWS, prefer managed services over Kubernetes, want zero-secret-rotation via IAM. The reference deployment. |
| [Helm chart](../../../charts/iac-cartographer/) | You're on Kubernetes. EKS / GKE / AKS / on-prem — all work. |
| [GitHub Actions](../../runtime/github-actions.yml) | No infrastructure to own; secrets live in the GitHub repo. Free for public repos. |
| [Cloud Run Job](../gcp-cloud-run-job/) | You're on GCP. Charges per vCPU-second. |
| [Container Apps Job](../azure-container-apps-job/) | You're on Azure. AAD / Managed Identity integration. |
| [Plain cron](../cron.sh) | Single VM you already own. No cloud account required. |
