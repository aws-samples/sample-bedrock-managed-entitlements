terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80.0, < 7.0.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0, < 3.0.0"
    }
  }
}

# Marketplace agreement events and License Manager licenses only exist in
# us-east-1, so the region is fixed rather than configurable. Credentials come
# from the standard AWS provider chain (AWS_PROFILE, SSO, environment, ...).
provider "aws" {
  region = "us-east-1"

  default_tags {
    tags = merge(
      {
        Project   = "sample-bedrock-managed-entitlements"
        ManagedBy = "terraform"
      },
      var.tags,
    )
  }
}
