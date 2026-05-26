output "job_name" {
  value       = azapi_resource.job.name
  description = "Container Apps Job name; trigger an ad-hoc execution with `az containerapp job start`."
}

output "key_vault_name" {
  value       = azurerm_key_vault.this.name
  description = "Key Vault holding the config + credential secrets."
}

output "identity_client_id" {
  value       = azurerm_user_assigned_identity.runner.client_id
  description = "Client ID of the User Assigned Identity. Useful for cross-cloud Workload Identity Federation."
}

output "credential_secret_names" {
  value       = { for k, v in azurerm_key_vault_secret.credentials : k => v.name }
  description = "Map of logical credential names → Key Vault secret names. After apply, populate each with `az keyvault secret set`."
}

output "config_secret_name" {
  value       = azurerm_key_vault_secret.config.name
  description = "Key Vault secret holding the rendered config.yaml. Update with `az keyvault secret set` to roll out a config change."
}

output "log_analytics_workspace_id" {
  value       = azurerm_log_analytics_workspace.this.id
  description = "Log destination for the Container Apps env. Tail with `az containerapp job logs show`."
}
