terraform {
  required_version = ">= 1.5"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.0"
    }
    azapi = {
      # Container Apps Jobs require a few properties that the azurerm
      # provider doesn't surface cleanly yet (workload profile binding,
      # secret refs from Key Vault). The azapi provider lets us reach
      # through to the underlying ARM API for those edges without
      # forking the resource definitions.
      source  = "Azure/azapi"
      version = ">= 2.0"
    }
  }
}
