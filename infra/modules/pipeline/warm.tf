# Warm-start controls for ingest and sender (docs/design.md section 9).
#
#   provisioned_concurrency_mode = "always"        -> 1 provisioned instance on the live alias, 24/7
#   provisioned_concurrency_mode = "market_hours"  -> EventBridge Scheduler calls the Lambda API directly
#                                                    (universal targets) at open and close, in market_timezone
#   keepalive_minutes > 0                           -> scheduled {"keepalive": true} invocations (cheap, best-effort)

locals {
  pc_always       = var.provisioned_concurrency_mode == "always" ? local.warm_functions : toset([])
  pc_market_hours = var.provisioned_concurrency_mode == "market_hours" ? local.warm_functions : toset([])
  keepalive       = var.keepalive_minutes > 0 ? local.warm_functions : toset([])
  needs_scheduler = length(local.pc_market_hours) + length(local.keepalive) > 0
}

resource "aws_lambda_provisioned_concurrency_config" "always" {
  for_each                          = local.pc_always
  function_name                     = aws_lambda_function.fn[each.key].function_name
  qualifier                         = aws_lambda_alias.live[each.key].name
  provisioned_concurrent_executions = 1
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_iam_policy_document" "scheduler" {
  count = local.needs_scheduler ? 1 : 0

  statement {
    sid       = "Keepalive"
    actions   = ["lambda:InvokeFunction"]
    resources = [for f in local.warm_functions : aws_lambda_alias.live[f].arn]
  }

  statement {
    sid     = "ProvisionedConcurrency"
    actions = ["lambda:PutProvisionedConcurrencyConfig", "lambda:DeleteProvisionedConcurrencyConfig"]
    resources = flatten([for f in local.warm_functions : [
      aws_lambda_function.fn[f].arn, "${aws_lambda_function.fn[f].arn}:*"
    ]])
  }
}

resource "aws_iam_role" "scheduler" {
  count              = local.needs_scheduler ? 1 : 0
  name               = "${var.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  count  = local.needs_scheduler ? 1 : 0
  name   = "${var.name_prefix}-scheduler"
  role   = aws_iam_role.scheduler[0].id
  policy = data.aws_iam_policy_document.scheduler[0].json
}

resource "aws_scheduler_schedule" "pc_on" {
  #checkov:skip=CKV_AWS_297:Schedule input holds only function names or a keepalive flag; nothing sensitive to encrypt
  for_each                     = local.pc_market_hours
  name                         = "${var.name_prefix}-${each.key}-pc-on"
  schedule_expression          = var.market_open_cron
  schedule_expression_timezone = var.market_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:lambda:putProvisionedConcurrencyConfig"
    role_arn = aws_iam_role.scheduler[0].arn
    input = jsonencode({
      FunctionName                    = aws_lambda_function.fn[each.key].function_name
      Qualifier                       = aws_lambda_alias.live[each.key].name
      ProvisionedConcurrentExecutions = 1
    })
  }
}

resource "aws_scheduler_schedule" "pc_off" {
  #checkov:skip=CKV_AWS_297:Schedule input holds only function names or a keepalive flag; nothing sensitive to encrypt
  for_each                     = local.pc_market_hours
  name                         = "${var.name_prefix}-${each.key}-pc-off"
  schedule_expression          = var.market_close_cron
  schedule_expression_timezone = var.market_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:lambda:deleteProvisionedConcurrencyConfig"
    role_arn = aws_iam_role.scheduler[0].arn
    input = jsonencode({
      FunctionName = aws_lambda_function.fn[each.key].function_name
      Qualifier    = aws_lambda_alias.live[each.key].name
    })
  }
}

resource "aws_scheduler_schedule" "keepalive" {
  #checkov:skip=CKV_AWS_297:Schedule input holds only function names or a keepalive flag; nothing sensitive to encrypt
  for_each            = local.keepalive
  name                = "${var.name_prefix}-${each.key}-keepalive"
  schedule_expression = "rate(${var.keepalive_minutes} minutes)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_alias.live[each.key].arn
    role_arn = aws_iam_role.scheduler[0].arn
    input    = jsonencode({ keepalive = true })

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
