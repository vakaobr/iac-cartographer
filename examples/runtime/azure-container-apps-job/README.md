# Azure — Container Apps Jobs

Terraform module deploying iac-cartographer on Azure as a Container
Apps Job with a built-in cron schedule. Suits any Azure subscription
that already uses Container Apps; serverless billing model — pay only
while the Job is running.

## Layout

```
.
├── versions.tf                # Terraform + provider pins (azurerm + azapi)
├── variables.tf               # resource_group, location, tenant, schedule, …
├── main.tf                    # the actual resources
├── outputs.tf                 # job_name, key_vault_name, identity_client_id, …
├── terraform.tfvars.example   # copy + edit before apply
└── README.md                  # this file
```

Resources created:

- `azurerm_user_assigned_identity.runner` — dedicated identity used by
  the Container Apps Job to read Key Vault secrets.
- `azurerm_key_vault.this` — credential + config storage (RBAC-authz
  mode; the runner identity gets `Key Vault Secrets User`).
- `azurerm_key_vault_secret.config` — the `config.yaml` body, versioned.
- `azurerm_key_vault_secret.credentials` × 5 — credential bundles
  (`confluence`, `gitlab`, `github`, `slack`, `anthropic`).
- `azurerm_role_assignment.runner_secrets_user` — IAM binding.
- `azurerm_log_analytics_workspace` + `azurerm_container_app_environment`
  — the parent env for the Job, log destination.
- `azapi_resource.job` (`Microsoft.App/jobs@2024-03-01`) — the actual
  Container Apps Job. Uses the `azapi` provider rather than `azurerm`
  because Container Apps Job + Key Vault secret refs aren't first-class
  in `azurerm` as of v4.x.

## Why azapi?

The `azurerm_container_app_job` resource exists but has gaps around
Key Vault-backed secret references. Using `azapi_resource` to hit the
underlying ARM API directly avoids forking the resource or building a
plain `value`-based Secret model. As `azurerm` catches up, this module
can switch back without breaking changes for users — the underlying
Azure resource is the same.

## Apply

```bash
cd examples/runtime/azure-container-apps-job

cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars
# At minimum: resource_group_name, tenant_id, config_yaml.parent_page_id.

az login                                   # if not already
az account set --subscription <subscription-id>

terraform init
terraform plan
terraform apply
```

The apply creates the credential secrets with a placeholder value
(`REPLACE_ME-via-az-cli`). The `lifecycle.ignore_changes = [value]`
block on those secrets means subsequent applies won't try to reset
them — the operator manages versions out-of-band.

## Populate the credentials (one-off, after apply)

```bash
KV=$(terraform output -raw key_vault_name)

az keyvault secret set --vault-name "$KV" --name iac-cartographer-confluence \
  --value '{"email":"bot@x","api_token":"ATATT..."}'

az keyvault secret set --vault-name "$KV" --name iac-cartographer-gitlab \
  --value '{"token":"glpat-..."}'

az keyvault secret set --vault-name "$KV" --name iac-cartographer-github \
  --value '{"token":"ghp_..."}'

az keyvault secret set --vault-name "$KV" --name iac-cartographer-slack \
  --value '{"bot_token":"xoxb-..."}'

az keyvault secret set --vault-name "$KV" --name iac-cartographer-anthropic \
  --value '{"api_key":"sk-ant-..."}'
```

Each secret in this module's `secrets[]` Job-spec entry references the
secret's `versionless_id` — when you add a new version with
`az keyvault secret set`, the next Job execution picks it up
automatically without a re-apply.

## Trigger a one-shot run

```bash
az containerapp job start \
  --name iac-cartographer \
  --resource-group <rg-name>
```

## Inspect logs

```bash
az containerapp job logs show \
  --name iac-cartographer \
  --resource-group <rg-name> \
  --follow
```

Or via the Azure Portal: *Container App Jobs → iac-cartographer →
Execution history → \<execution\> → Console logs*.

## Roll out a config change

Update the `config_yaml` variable, then `terraform apply`. The
`azurerm_key_vault_secret.config` resource adds a new version; the
next Job execution picks it up automatically.

For a faster roll without a Terraform apply:

```bash
KV=$(terraform output -raw key_vault_name)
az keyvault secret set --vault-name "$KV" --name iac-cartographer-config \
  --file path/to/new-config.yaml --encoding utf-8
```

## Why this shape

- **Container Apps Jobs vs Container Apps services.** Services are
  request-driven and stay warm; Jobs are batch-driven and exit. The
  CLI is `iac-cartographer --once`, which exits — Jobs is the right
  primitive.
- **Native cron on the Job vs Logic Apps schedule.** Container Apps
  Jobs have a built-in `Schedule` trigger that takes a cron expression
  — no extra Logic App or Azure Function needed.
- **User Assigned Identity vs System Assigned.** UA identity lives in
  the resource group separately from the Job, which means: the same
  identity can be reused across multiple environments / subscriptions,
  and rotating the Job (delete + recreate) doesn't lose the IAM
  bindings.
- **Key Vault with RBAC authz, not access policies.** Access policies
  are the legacy model; RBAC is the modern one and integrates with
  Azure-native role assignments. Required for the `secrets[*].identity`
  field on the Job spec to work cleanly.

## Cost

Typical run for ~50 repos:

- Container Apps Job (Consumption plan): ~5 minutes × 1 vCPU × 2 GiB ≈
  **$0.005**.
- Key Vault: **~$0.03/month** for 6 secrets (you only pay for ops,
  not storage; the first 10K ops/month are ~$0.03).
- Log Analytics: bundled into Azure Monitor's free tier for typical
  volumes (5 GB/day free).

Weekly runs ≈ **$0.02/month** in compute. The LLM call (Anthropic) is
typically the dominant cost.

## Switching LLM to Azure OpenAI

The Anthropic backend is the default in this example because the
Azure OpenAI iac-cartographer LLM backend is still on the
[roadmap](https://github.com/vakaobr/iac-cartographer#coming-next).
Once shipped, swap `llm.backend: anthropic` for
`llm.backend: azure_openai` in `config_yaml`. The User Assigned
Identity will need `Cognitive Services User` on the Azure OpenAI
resource.

For now, if you want the LLM call to stay within your Azure tenant,
deploy Anthropic via a private endpoint behind Azure Front Door and
override `anthropic_base_url` in the config.
