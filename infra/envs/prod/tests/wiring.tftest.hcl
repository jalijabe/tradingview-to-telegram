# terraform test (mock AWS providers; no credentials needed)

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

mock_provider "aws" {
  alias = "observer"
  mock_data "aws_region" {
    defaults = { region = "us-east-2" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
  mock_data "aws_subnets" {
    defaults = { ids = ["subnet-0a"] }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0default" }
  }
}

variables {
  chat_id        = "-1001"
  canary_chat_id = "-1002"
}

run "no_observer_by_default" {
  command = plan
  assert {
    condition     = length(module.observer_ec2) == 0 && output.observer.host == "none" && output.observer.instance_id == null
    error_message = "observer_host defaults to none"
  }
}

run "external_observer_creates_no_aws_host" {
  command = plan
  variables {
    observer_host = "external"
  }
  assert {
    condition     = length(module.observer_ec2) == 0 && output.observer.host == "external"
    error_message = "external hosts (Pi, Docker) need no AWS resources"
  }
}

run "ec2_observer_in_its_own_region" {
  command = plan
  variables {
    observer_host   = "ec2"
    observer_region = "us-east-2"
  }

  # The collector URL is only known after apply; stub the pipeline's outputs so the user data renders at plan.
  override_module {
    target          = module.pipeline
    override_during = plan
    outputs = {
      ingest_url      = "https://ingest.example/"
      collector_url   = "https://collector.example/"
      function_names  = { ingest = "tvtg-prod-ingest", sender = "tvtg-prod-sender", collector = "tvtg-prod-collector" }
      log_group_names = { ingest = "/aws/lambda/tvtg-prod-ingest", sender = "/aws/lambda/tvtg-prod-sender", collector = "/aws/lambda/tvtg-prod-collector" }
      dlq_name        = "tvtg-prod-alerts-dlq"
      queue_name      = "tvtg-prod-alerts"
      timing_table    = "tvtg-prod-timing"
      ssm_parameters  = { tv_secret = "/tvtg-prod/tv_secret", bot_token = "/tvtg-prod/bot_token", collector_key = "/tvtg-prod/collector_key", allowed_ips = "/tvtg-prod/allowed_ips" }
    }
  }
  assert {
    condition     = length(module.observer_ec2) == 1 && module.observer_ec2[0].region == "us-east-2"
    error_message = "ec2 observer uses the observer provider/region"
  }
  assert {
    condition     = module.observer_ec2[0].ssm_prefix == "/tvtg-prod/observer"
    error_message = "observer secrets under the env prefix"
  }
  assert {
    condition = alltrue([for s in [
      "CHAT_IDS=-1001,-1002",
      "COLLECTOR_URL=https://collector.example/",
      "COLLECTOR_KEY_SSM=/tvtg-prod/collector_key", # pragma: allowlist secret (parameter name)
      "COLLECTOR_KEY_SSM_REGION=us-west-2",
      "AWS_REGION=us-east-2",
    ] : strcontains(module.observer_ec2[0].user_data, s)])
    error_message = "observer watches both channels and reads the collector key from the pipeline region"
  }
}

run "rejects_unknown_observer_host" {
  command = plan
  variables {
    observer_host = "laptop"
  }
  expect_failures = [var.observer_host]
}
