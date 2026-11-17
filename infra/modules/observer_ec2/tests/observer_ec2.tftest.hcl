# terraform test (mock AWS provider; no credentials needed)

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { region = "us-east-2" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
  mock_data "aws_vpc" {
    defaults = { id = "vpc-0default" }
  }
  mock_data "aws_subnets" {
    defaults = { ids = ["subnet-0b", "subnet-0a"] }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0default" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  name_prefix         = "tvtg-test"
  chat_ids            = ["-1001", "-1002"]
  collector_url       = "https://collector.example/"
  collector_key_param = "/tvtg-test/collector_key"
  pipeline_region     = "us-west-2"
}

run "defaults_t4g_nano_in_default_vpc" {
  command = plan

  assert {
    condition     = aws_instance.observer.instance_type == "t4g.nano"
    error_message = "default instance type should be t4g.nano"
  }
  assert {
    condition     = data.aws_ssm_parameter.ami.name == "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
    error_message = "arm64 AL2023 AMI expected"
  }
  assert {
    condition     = length(data.aws_vpc.default) == 1 && data.aws_subnet.selected.id == "subnet-0a"
    error_message = "should pick the first (sorted) default-VPC subnet"
  }
  assert {
    condition     = aws_instance.observer.associate_public_ip_address && aws_instance.observer.metadata_options[0].http_tokens == "required"
    error_message = "public IP for egress, IMDSv2 required"
  }
  assert {
    condition     = length(aws_instance.observer.credit_specification) == 1
    error_message = "burstable type should pin standard credits"
  }
  assert {
    condition     = toset(keys(aws_ssm_parameter.secret)) == toset(["api_id", "api_hash", "session"])
    error_message = "three observer secrets"
  }
  assert {
    condition     = aws_ssm_parameter.secret["session"].name == "/tvtg-test/observer/session" && aws_ssm_parameter.secret["session"].type == "SecureString"
    error_message = "session parameter name/type"
  }
  assert {
    condition = alltrue([for s in [
      "OBSERVER_NAME=ec2-us-east-2",
      "CHAT_IDS=-1001,-1002",
      "COLLECTOR_URL=https://collector.example/",
      "OBSERVER_SSM_PREFIX=/tvtg-test/observer",
      "AWS_REGION=us-east-2",
      "COLLECTOR_KEY_SSM=/tvtg-test/collector_key",
      "COLLECTOR_KEY_SSM_REGION=us-west-2",
      "install.sh\" --src \"$SRC\" --env-file /etc/tvtg-observer.env --with-aws --pkg dnf",
      "def make_handler(",
    ] : strcontains(output.user_data, s)])
    error_message = "rendered user data is missing expected content"
  }
  assert {
    condition     = length(aws_instance.observer.user_data_base64) * 3 / 4 < 16384
    error_message = "compressed user data must stay under the 16 KB EC2 limit"
  }
}

run "custom_subnet_x86_named" {
  command = plan
  variables {
    subnet_id           = "subnet-0custom"
    instance_type       = "t3a.nano"
    architecture        = "x86_64"
    observer_name       = "nyc"
    associate_public_ip = false
  }

  assert {
    condition     = length(data.aws_vpc.default) == 0 && data.aws_subnet.selected.id == "subnet-0custom"
    error_message = "explicit subnet should skip the default VPC lookup"
  }
  assert {
    condition     = endswith(data.aws_ssm_parameter.ami.name, "-x86_64") && !aws_instance.observer.associate_public_ip_address
    error_message = "x86 AMI and no public IP"
  }
  assert {
    condition     = strcontains(output.user_data, "OBSERVER_NAME=nyc")
    error_message = "observer_name should label the host"
  }
}

run "non_burstable_type_has_no_credit_spec" {
  command = plan
  variables {
    instance_type = "m7g.medium"
  }
  assert {
    condition     = length(aws_instance.observer.credit_specification) == 0
    error_message = "credit_specification only applies to t* types"
  }
}

run "rejects_unknown_architecture" {
  command = plan
  variables {
    architecture = "sparc"
  }
  expect_failures = [var.architecture]
}
