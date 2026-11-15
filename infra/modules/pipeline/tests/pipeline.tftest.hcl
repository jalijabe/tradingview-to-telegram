# terraform test (mock AWS provider; no credentials needed)

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  name_prefix = "tvtg-test"
  src_dir     = "../../../src"
  chat_id     = "-1001"
}

run "defaults" {
  command = plan

  assert {
    condition     = toset(keys(aws_lambda_function.fn)) == toset(["ingest", "sender", "collector"])
    error_message = "three functions"
  }
  assert {
    condition     = alltrue([for f in aws_lambda_function.fn : f.publish && f.runtime == "python3.12"])
    error_message = "functions must publish versions (for the alias) on python3.12"
  }
  assert {
    condition     = aws_lambda_function_url.public["ingest"].qualifier == "live" && aws_lambda_function_url.public["collector"].qualifier == "live"
    error_message = "public URLs must target the live alias"
  }
  assert {
    condition     = aws_lambda_function.fn["ingest"].environment[0].variables["ALLOW_ANY_IP"] == "false"
    error_message = "IP allowlist on by default"
  }
  assert {
    condition     = aws_lambda_function.fn["sender"].environment[0].variables["CHAT_ID"] == "-1001" && aws_lambda_function.fn["sender"].environment[0].variables["SECRETS_REVISION"] == "1"
    error_message = "sender env"
  }
  assert {
    condition     = aws_lambda_event_source_mapping.sender.batch_size == 1 && aws_lambda_event_source_mapping.sender.scaling_config[0].maximum_concurrency == 2
    error_message = "sender trigger: batch 1, max concurrency 2"
  }
  assert {
    condition     = aws_sqs_queue.alerts.visibility_timeout_seconds >= 6 * aws_lambda_function.fn["sender"].timeout
    error_message = "visibility timeout must be at least 6x the sender timeout"
  }
  assert {
    condition     = length(aws_lambda_provisioned_concurrency_config.always) + length(aws_scheduler_schedule.pc_on) + length(aws_scheduler_schedule.keepalive) + length(aws_iam_role.scheduler) == 0
    error_message = "nothing warm by default"
  }
}

run "pc_always" {
  command = plan
  variables {
    provisioned_concurrency_mode = "always"
  }
  assert {
    condition     = toset(keys(aws_lambda_provisioned_concurrency_config.always)) == toset(["ingest", "sender"])
    error_message = "ingest and sender get provisioned concurrency; collector doesn't"
  }
  assert {
    condition     = aws_lambda_provisioned_concurrency_config.always["sender"].qualifier == "live"
    error_message = "provisioned concurrency must be on the alias, never $LATEST"
  }
  assert {
    condition     = length(aws_scheduler_schedule.pc_on) == 0 && length(aws_iam_role.scheduler) == 0
    error_message = "no schedules in always mode"
  }
}

run "pc_market_hours" {
  command = plan
  variables {
    provisioned_concurrency_mode = "market_hours"
  }
  assert {
    condition     = length(aws_scheduler_schedule.pc_on) == 2 && length(aws_scheduler_schedule.pc_off) == 2 && length(aws_lambda_provisioned_concurrency_config.always) == 0
    error_message = "on/off schedule per warm function"
  }
  assert {
    condition     = aws_scheduler_schedule.pc_on["ingest"].schedule_expression_timezone == "America/New_York" && aws_scheduler_schedule.pc_on["ingest"].schedule_expression == "cron(20 9 ? * MON-FRI *)"
    error_message = "opens 09:20 New York time"
  }
  assert {
    condition     = endswith(aws_scheduler_schedule.pc_on["sender"].target[0].arn, ":putProvisionedConcurrencyConfig") && endswith(aws_scheduler_schedule.pc_off["sender"].target[0].arn, ":deleteProvisionedConcurrencyConfig")
    error_message = "universal targets call the provisioned-concurrency APIs"
  }
  assert {
    condition     = length(aws_iam_role.scheduler) == 1
    error_message = "scheduler role"
  }
}

run "keepalive" {
  command = plan
  variables {
    keepalive_minutes = 3
  }
  assert {
    condition     = length(aws_scheduler_schedule.keepalive) == 2 && aws_scheduler_schedule.keepalive["sender"].schedule_expression == "rate(3 minutes)"
    error_message = "keepalive every 3 minutes for ingest and sender"
  }
  assert {
    condition     = jsondecode(aws_scheduler_schedule.keepalive["ingest"].target[0].input) == { keepalive = true }
    error_message = "keepalive payload"
  }
}

run "rejects_unknown_pc_mode" {
  command = plan
  variables {
    provisioned_concurrency_mode = "sometimes"
  }
  expect_failures = [var.provisioned_concurrency_mode]
}
