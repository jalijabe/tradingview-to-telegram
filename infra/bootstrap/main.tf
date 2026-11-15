# One-time setup, applied by hand with admin credentials (docs/deploy.md step 1). Local state, on purpose:
# this stack creates the bucket that every other stack keeps its state in.
#
#   terraform init && terraform apply -var github_repo=jalijabe/tradingview-to-telegram -var state_bucket=UNIQUE-NAME

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "github_repo" {
  description = "owner/repo allowed to assume the CI roles, e.g. jalijabe/tradingview-to-telegram."
  type        = string
}

variable "state_bucket" {
  description = "Globally unique S3 bucket name for Terraform state."
  type        = string
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "tv-tg-bot", managed_by = "terraform-bootstrap" }
  }
}

# ----------------------------------------------------------------------------- state bucket

resource "aws_s3_bucket" "state" {
  #checkov:skip=CKV_AWS_18:Access logs for a single-user state bucket would need a second bucket; CloudTrail records API access
  #checkov:skip=CKV_AWS_144:Cross-region replication is out of scope; versioning plus lifecycle protects against bad writes
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256) is sufficient for this state; a KMS CMK adds cost without a new control
  #checkov:skip=CKV2_AWS_62:Nothing consumes bucket event notifications
  bucket = var.state_bucket
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ----------------------------------------------------------------------------- GitHub OIDC

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "trust" {
  for_each = {
    # Pull requests: plan only (read-only + state lock file).
    plan = ["repo:${var.github_repo}:pull_request"]
    # main branch (dev) and the protected prod environment (needs a reviewer's approval).
    deploy = ["repo:${var.github_repo}:ref:refs/heads/main", "repo:${var.github_repo}:environment:prod",
    "repo:${var.github_repo}:environment:dev"]
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = each.value
    }
  }
}

resource "aws_iam_role" "ci" {
  for_each             = data.aws_iam_policy_document.trust
  name                 = "tvtg-github-${each.key}"
  assume_role_policy   = each.value.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "state" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
  }
}

resource "aws_iam_role_policy" "state" {
  for_each = aws_iam_role.ci
  name     = "terraform-state"
  role     = each.value.id
  policy   = data.aws_iam_policy_document.state.json
}

resource "aws_iam_role_policy_attachment" "plan_readonly" {
  role       = aws_iam_role.ci["plan"].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# A single-purpose account is assumed. Narrow this to the services in infra/modules if the account is shared.
resource "aws_iam_role_policy_attachment" "deploy_admin" {
  #checkov:skip=CKV_AWS_274:Single-purpose account; the role is assumable only via GitHub OIDC from main or approved environments. Narrow it if the account is shared
  role       = aws_iam_role.ci["deploy"].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "plan_role_arn" {
  value = aws_iam_role.ci["plan"].arn
}

output "deploy_role_arn" {
  value = aws_iam_role.ci["deploy"].arn
}
