# Container Apps Job deployment for iac-cartographer on Azure.
#
# Shape:
#   * azurerm_user_assigned_identity     — dedicated identity for the Job.
#   * azurerm_key_vault                  — credential + config storage.
#   * azurerm_key_vault_secret           — config.yaml + 5 credential bundles.
#   * azurerm_role_assignment            — grants the identity Key Vault Secrets User.
#   * azurerm_log_analytics_workspace    — log destination for the Container Apps env.
#   * azurerm_container_app_environment  — the parent env for the Job.
#   * azapi_resource (Microsoft.App/jobs) — the Container Apps Job itself.
#                                           Uses azapi rather than azurerm so
#                                           Key Vault secret refs work natively.
#
# Container Apps Jobs natively support Key Vault-backed secrets via the
# `secrets[*].keyVaultUrl` field, which iac-cartographer's env secrets
# backend reads as plain env vars. Same shape as the GCP example
# (env-backend + Secret Manager); platform-specific glue lives in
# the Job spec.

locals {
  base_tags = merge(
    {
      app        = var.name
      managed-by = "terraform"
    },
    var.tags,
  )

  # Logical secret names → Key Vault secret names. The KV names follow
  # the env-var mangling rule used elsewhere so the mapping is mechanical.
  secrets = {
    confluence = "${var.name}-confluence"
    gitlab     = "${var.name}-gitlab"
    github     = "${var.name}-github"
    slack      = "${var.name}-slack"
    anthropic  = "${var.name}-anthropic"
  }
}

# ─── Identity ──────────────────────────────────────────────────────────

resource "azurerm_user_assigned_identity" "runner" {
  name                = var.name
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = local.base_tags
}

# ─── Key Vault (secrets + config storage) ──────────────────────────────

resource "azurerm_key_vault" "this" {
  name                       = substr("${var.name}-${substr(sha1(var.resource_group_name), 0, 8)}", 0, 24)
  location                   = var.location
  resource_group_name        = var.resource_group_name
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  enable_rbac_authorization  = true
  purge_protection_enabled   = false
  soft_delete_retention_days = 7
  tags                       = local.base_tags
}

# Grant the runner identity read access to all secrets in the vault.
# Using a single broad assignment rather than per-secret grants keeps
# the IAM surface small and matches the GCP example's scope.
resource "azurerm_role_assignment" "runner_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.runner.principal_id
}

# Config YAML — versioned in Key Vault like the credentials, so a config
# change is `terraform apply` → new version + the next Job run picks it up.
resource "azurerm_key_vault_secret" "config" {
  name         = "${var.name}-config"
  value        = var.config_yaml
  key_vault_id = azurerm_key_vault.this.id
  tags         = local.base_tags

  depends_on = [azurerm_role_assignment.runner_secrets_user]
}

# Credential secrets — created with a placeholder value the operator
# replaces post-apply. Same pattern as the GCP example; keeps real
# tokens out of Terraform state.
resource "azurerm_key_vault_secret" "credentials" {
  for_each = local.secrets

  name         = each.value
  value        = "REPLACE_ME-via-az-cli"
  key_vault_id = azurerm_key_vault.this.id
  tags         = local.base_tags

  # Ignore changes to the value — operator manages versions out-of-band
  # via `az keyvault secret set`. Otherwise every Terraform apply would
  # try to reset the placeholder.
  lifecycle {
    ignore_changes = [value, version]
  }

  depends_on = [azurerm_role_assignment.runner_secrets_user]
}

# ─── Logs + Container Apps environment ─────────────────────────────────

resource "azurerm_log_analytics_workspace" "this" {
  name                = "${var.name}-logs"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.base_tags
}

resource "azurerm_container_app_environment" "this" {
  name                       = "${var.name}-env"
  location                   = var.location
  resource_group_name        = var.resource_group_name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  tags                       = local.base_tags
}

# ─── The Container Apps Job ────────────────────────────────────────────

# Container Apps Jobs are not yet first-class in the azurerm provider as
# of v4.x — `azurerm_container_app_job` exists but has gaps around Key
# Vault-backed secrets. The azapi provider lets us reach through to the
# underlying ARM API and define the resource as-is.
resource "azapi_resource" "job" {
  type      = "Microsoft.App/jobs@2024-03-01"
  name      = var.name
  parent_id = "/subscriptions/${data.azurerm_subscription.current.subscription_id}/resourceGroups/${var.resource_group_name}"
  location  = var.location
  tags      = local.base_tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runner.id]
  }

  body = {
    properties = {
      environmentId = azurerm_container_app_environment.this.id

      configuration = {
        triggerType = "Schedule"
        scheduleTriggerConfig = {
          cronExpression = var.schedule
          # Match the k8s example's Forbid policy + 0 backoff.
          parallelism            = 1
          replicaCompletionCount = 1
        }
        replicaTimeout    = var.replica_timeout
        replicaRetryLimit = var.replica_retry_limit

        # Secrets sourced from Key Vault. The `identity` field tells
        # Container Apps which managed identity to use when resolving
        # the keyVaultUrl. The `name` here is the in-container secret
        # alias referenced by env.secretRef below.
        secrets = concat(
          [
            {
              name        = "config-yaml"
              keyVaultUrl = azurerm_key_vault_secret.config.versionless_id
              identity    = azurerm_user_assigned_identity.runner.id
            },
          ],
          [
            for k, v in local.secrets : {
              name        = "secret-${k}"
              keyVaultUrl = azurerm_key_vault_secret.credentials[k].versionless_id
              identity    = azurerm_user_assigned_identity.runner.id
            }
          ],
        )
      }

      template = {
        containers = [
          {
            name  = var.name
            image = var.image
            # Container Apps Jobs don't support file-mounted secrets the
            # way k8s does — only env-var-backed. So we point the CLI at
            # /dev/stdin via a wrapper, OR pass --config as a literal
            # string read from an env var. Simpler: write the config to
            # /tmp at startup via the standard shell trick.
            #
            # Container Apps' `args` are appended to the image's
            # ENTRYPOINT (`iac-cartographer`). We override the entrypoint
            # so we can `sh -c '...'` to materialise the config file
            # before running the CLI.
            command = ["/bin/sh", "-c"]
            args = [
              "printf '%s' \"$IAC_CARTOGRAPHER_CONFIG_YAML\" > /tmp/config.yaml && iac-cartographer --once --config /tmp/config.yaml",
            ]

            resources = {
              cpu    = var.cpu
              memory = var.memory
            }

            env = concat(
              [
                {
                  name      = "IAC_CARTOGRAPHER_CONFIG_YAML"
                  secretRef = "config-yaml"
                },
              ],
              [
                for k, v in local.secrets : {
                  name      = "IAC_CARTOGRAPHER_SECRET_${upper(k)}"
                  secretRef = "secret-${k}"
                }
              ],
            )
          }
        ]
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.runner_secrets_user,
    azurerm_key_vault_secret.config,
    azurerm_key_vault_secret.credentials,
  ]
}

data "azurerm_subscription" "current" {}
