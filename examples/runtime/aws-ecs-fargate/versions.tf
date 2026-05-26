terraform {
  required_version = ">= 1.6"

  required_providers {
    # AWS provider 5.x covers EventBridge Scheduler (introduced in
    # 5.10) — the modern scheduler path we use here. Pin to a major
    # so an unexpected 6.x bump doesn't break the apply.
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.10, < 6.0"
    }
  }
}
