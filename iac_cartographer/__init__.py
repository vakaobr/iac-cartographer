"""iac-cartographer — fleet-level documentation for your Terraform / IaC estate.

Discovers Terraform repositories across GitLab and GitHub, extracts structural
facts with `terraform-docs` (and a fallback HCL parser for fields terraform-docs
strips), narrates the purpose of each repo with a Claude model on AWS Bedrock,
and publishes a parent + child page hierarchy to Confluence. State-free via a
banner-SHA short-circuit on each published page; idempotent under re-runs.

Runs as a scheduled batch job (ECS Fargate, Kubernetes CronJob, GitHub Actions
schedule, plain cron — runtime is your call).
"""

__version__ = "0.1.7"
