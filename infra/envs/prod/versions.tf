terraform {
  required_version = ">= 1.10" # S3 native state locking (use_lockfile)

  # Partial config; the bucket comes from -backend-config (see docs/deploy.md and .github/workflows/deploy.yml).
  backend "s3" {
    region       = "us-west-2"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4, < 3.0"
    }
  }
}
