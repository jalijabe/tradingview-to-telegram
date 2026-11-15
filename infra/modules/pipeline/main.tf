terraform {
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

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  ssm_prefix = "/${var.name_prefix}"

  functions = {
    ingest = {
      handler = "tvtg.ingest.handler"
      timeout = 5 # longer than TradingView's 3 s: an alert still gets queued even if TradingView gave up
      memory  = var.memory_mb
      env = {
        QUEUE_URL         = aws_sqs_queue.alerts.url
        TV_SECRET_PARAM   = aws_ssm_parameter.tv_secret.name
        ALLOWED_IPS_PARAM = aws_ssm_parameter.allowed_ips.name
        ALLOW_ANY_IP      = tostring(var.allow_any_ip)
      }
    }
    sender = {
      handler = "tvtg.sender.handler"
      timeout = 15
      memory  = var.memory_mb
      env = {
        BOT_TOKEN_PARAM = aws_ssm_parameter.bot_token.name
        CHAT_ID         = var.chat_id
        CANARY_CHAT_ID  = var.canary_chat_id
        QUEUE_URL       = aws_sqs_queue.alerts.url
        TIMING_TABLE    = aws_dynamodb_table.timing.name
      }
    }
    collector = {
      handler = "tvtg.collector.handler"
      timeout = 5
      memory  = 128
      env = {
        COLLECTOR_KEY_PARAM = aws_ssm_parameter.collector_key.name
        TIMING_TABLE        = aws_dynamodb_table.timing.name
      }
    }
  }

  warm_functions = toset(["ingest", "sender"])
}

# ----------------------------------------------------------------------------- package

data "archive_file" "package" {
  type        = "zip"
  source_dir  = var.src_dir
  output_path = "${path.root}/.build/${var.name_prefix}-lambda.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

# ----------------------------------------------------------------------------- secrets
# Terraform creates placeholders. Set the real values with `aws ssm put-parameter --overwrite`
# (see docs/deploy.md), so secrets never live in Terraform state or the repo.

resource "aws_ssm_parameter" "tv_secret" {
  #checkov:skip=CKV_AWS_337:SecureString with the AWS-managed aws/ssm key; a CMK adds cost without a new access boundary beyond IAM
  name  = "${local.ssm_prefix}/tv_secret"
  type  = "SecureString"
  value = "CHANGE_ME"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "bot_token" {
  #checkov:skip=CKV_AWS_337:SecureString with the AWS-managed aws/ssm key; a CMK adds cost without a new access boundary beyond IAM
  name  = "${local.ssm_prefix}/bot_token"
  type  = "SecureString"
  value = "CHANGE_ME"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "collector_key" {
  #checkov:skip=CKV_AWS_337:SecureString with the AWS-managed aws/ssm key; a CMK adds cost without a new access boundary beyond IAM
  name  = "${local.ssm_prefix}/collector_key"
  type  = "SecureString"
  value = "CHANGE_ME"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "allowed_ips" {
  #checkov:skip=CKV2_AWS_34:TradingView's published webhook IPs are public information, not a secret
  name  = "${local.ssm_prefix}/allowed_ips"
  type  = "StringList"
  value = join(",", var.tradingview_ips)
  lifecycle { ignore_changes = [value] }
}

# ----------------------------------------------------------------------------- queue + timing table

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name_prefix}-alerts-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "alerts" {
  name                       = "${var.name_prefix}-alerts"
  visibility_timeout_seconds = 90 # 6x the sender timeout
  message_retention_seconds  = 345600
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_dynamodb_table" "timing" {
  #checkov:skip=CKV_AWS_119:Timing data only (no secrets or PII); the AWS-owned key is sufficient
  #checkov:skip=CKV_AWS_28:Records expire after 1 day and are re-creatable; point-in-time recovery adds nothing
  name         = "${var.name_prefix}-timing"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# ----------------------------------------------------------------------------- IAM (one role per function)

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "fn" {
  for_each = local.functions

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.fn[each.key].arn}:*"]
  }

  statement {
    sid     = "Secrets"
    actions = ["ssm:GetParameter"]
    resources = {
      ingest    = [aws_ssm_parameter.tv_secret.arn, aws_ssm_parameter.allowed_ips.arn]
      sender    = [aws_ssm_parameter.bot_token.arn]
      collector = [aws_ssm_parameter.collector_key.arn]
    }[each.key]
  }

  dynamic "statement" {
    for_each = each.key == "ingest" ? [1] : []
    content {
      sid       = "Enqueue"
      actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
      resources = [aws_sqs_queue.alerts.arn]
    }
  }

  dynamic "statement" {
    for_each = each.key == "sender" ? [1] : []
    content {
      sid = "Consume"
      actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility"]
      resources = [aws_sqs_queue.alerts.arn]
    }
  }

  dynamic "statement" {
    for_each = each.key == "sender" ? [1] : []
    content {
      sid       = "StoreTiming"
      actions   = ["dynamodb:PutItem"]
      resources = [aws_dynamodb_table.timing.arn]
    }
  }

  dynamic "statement" {
    for_each = each.key == "collector" ? [1] : []
    content {
      sid       = "ReadTiming"
      actions   = ["dynamodb:GetItem"]
      resources = [aws_dynamodb_table.timing.arn]
    }
  }
}

resource "aws_iam_role" "fn" {
  for_each           = local.functions
  name               = "${var.name_prefix}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "fn" {
  for_each = local.functions
  name     = "${var.name_prefix}-${each.key}"
  role     = aws_iam_role.fn[each.key].id
  policy   = data.aws_iam_policy_document.fn[each.key].json
}

# ----------------------------------------------------------------------------- functions

resource "aws_cloudwatch_log_group" "fn" {
  #checkov:skip=CKV_AWS_158:Logs hold no secrets (the secret is stripped before logging); default encryption is sufficient
  #checkov:skip=CKV_AWS_338:90-day retention is a deliberate cost choice (var.log_retention_days)
  for_each          = local.functions
  name              = "/aws/lambda/${var.name_prefix}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "fn" {
  #checkov:skip=CKV_AWS_116:No async invocations: URLs are synchronous and the sender's SQS queue has its own DLQ
  #checkov:skip=CKV_AWS_117:Needs internet egress (Telegram); a VPC would require a NAT gateway costing more than the whole stack
  #checkov:skip=CKV_AWS_173:Environment variables hold only parameter names and ids; secrets are read from SSM at runtime
  #checkov:skip=CKV_AWS_272:Single-owner repo deployed only by CI from main via OIDC; code signing adds no boundary here
  #checkov:skip=CKV_AWS_50:Per-hop timing is captured in structured logs; X-Ray cannot see TradingView or Telegram (design D5)
  for_each         = local.functions
  function_name    = "${var.name_prefix}-${each.key}"
  role             = aws_iam_role.fn[each.key].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = each.value.handler
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256
  publish          = true # provisioned concurrency needs a published version behind an alias

  reserved_concurrent_executions = each.key == "ingest" ? var.ingest_reserved_concurrency : -1

  environment {
    # SECRETS_REVISION: bump var.secrets_revision after rotating an SSM secret. The config change publishes a new
    # version and moves the alias, so every container starts fresh and re-reads its secrets.
    variables = merge(each.value.env, { SECRETS_REVISION = var.secrets_revision })
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.fn[each.key].name
  }

  depends_on = [aws_iam_role_policy.fn]
}

resource "aws_lambda_alias" "live" {
  for_each         = local.functions
  name             = "live"
  function_name    = aws_lambda_function.fn[each.key].function_name
  function_version = aws_lambda_function.fn[each.key].version
}

# ----------------------------------------------------------------------------- public endpoints (on the alias)

resource "aws_lambda_function_url" "public" {
  #checkov:skip=CKV_AWS_258:TradingView cannot sign IAM requests; auth is enforced in code (IP allowlist + secret, or HMAC)
  for_each           = toset(["ingest", "collector"])
  function_name      = aws_lambda_function.fn[each.key].function_name
  qualifier          = aws_lambda_alias.live[each.key].name
  authorization_type = "NONE" # auth happens in code: IP allowlist + secret / HMAC
}

resource "aws_lambda_permission" "public_url" {
  #checkov:skip=CKV_AWS_301:Public by design (see aws_lambda_function_url.public); requests are authenticated in code
  for_each               = toset(["ingest", "collector"])
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.fn[each.key].function_name
  qualifier              = aws_lambda_alias.live[each.key].name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# Function URLs created since late 2025 also need lambda:InvokeFunction, limited to calls via the URL.
resource "aws_lambda_permission" "public_url_invoke" {
  #checkov:skip=CKV_AWS_301:Limited to invocations via the function URL (invoked_via_function_url); authenticated in code
  for_each                 = toset(["ingest", "collector"])
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.fn[each.key].function_name
  qualifier                = aws_lambda_alias.live[each.key].name
  principal                = "*"
  invoked_via_function_url = true
}

# ----------------------------------------------------------------------------- queue -> sender (alias)

resource "aws_lambda_event_source_mapping" "sender" {
  event_source_arn                   = aws_sqs_queue.alerts.arn
  function_name                      = aws_lambda_alias.live["sender"].arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 2 # Telegram allows about 1 msg/s per chat
  }
}
