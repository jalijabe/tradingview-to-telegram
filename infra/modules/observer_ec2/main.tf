# Observer on a small EC2 instance (default t4g.nano), for when you don't want to run a machine yourself.
#
# - Amazon Linux 2023: chrony comes preconfigured against the Amazon Time Sync Service (sub-ms clock)
# - No inbound ports, no SSH key: shell access only through SSM Session Manager
# - Secrets live in SSM; the instance reads them at every service start, so setting or rotating one only
#   needs `sudo systemctl restart tvtg-observer` (or wait: the service retries every 30 s until configured)
# - Same observer.py, install.sh and systemd unit as a Raspberry Pi; cloud-init runs install.sh at boot
#
# Note: it measures delivery to an AWS data center in var.region, not to your home. Pick the region nearest you.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
}

variable "name_prefix" {
  description = "e.g. tvtg-prod; SSM parameters go under /<name_prefix>/observer/."
  type        = string
}

variable "instance_type" {
  type    = string
  default = "t4g.nano"
}

variable "architecture" {
  description = "arm64 for Graviton types (t4g, ...), x86_64 for t3/t3a."
  type        = string
  default     = "arm64"
  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be arm64 or x86_64."
  }
}

variable "subnet_id" {
  description = "Subnet with outbound internet access. Empty = a default-VPC subnet in this region."
  type        = string
  default     = ""
}

variable "associate_public_ip" {
  description = "Needed in default-VPC subnets for outbound access (~$3.65/month for the IPv4 address). False if the subnet has a NAT."
  type        = bool
  default     = true
}

variable "observer_name" {
  description = "Dashboard label. Empty = ec2-<region>."
  type        = string
  default     = ""
}

variable "chat_ids" {
  type = list(string)
}

variable "collector_url" {
  type = string
}

variable "collector_key_param" {
  description = "SSM parameter name of the collector HMAC key (in pipeline_region)."
  type        = string
}

variable "pipeline_region" {
  type = string
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-${var.architecture}"
}

data "aws_vpc" "default" {
  count   = var.subnet_id == "" ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.subnet_id == "" ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_subnet" "selected" {
  id = var.subnet_id != "" ? var.subnet_id : sort(data.aws_subnets.default[0].ids)[0]
}

locals {
  region     = data.aws_region.current.region
  account    = data.aws_caller_identity.current.account_id
  ssm_prefix = "/${var.name_prefix}/observer"
  name       = var.observer_name != "" ? var.observer_name : "ec2-${local.region}"
  src        = "${path.module}/../../../observer"

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    observer_py         = file("${local.src}/observer.py")
    install_sh          = file("${local.src}/install.sh")
    unit                = file("${local.src}/tvtg-observer.service")
    requirements        = file("${local.src}/requirements.txt")
    requirements_aws    = file("${local.src}/requirements-aws.txt")
    env_example         = file("${local.src}/observer.env.example")
    observer_name       = local.name
    chat_ids            = join(",", var.chat_ids)
    collector_url       = var.collector_url
    ssm_prefix          = local.ssm_prefix
    region              = local.region
    collector_key_param = var.collector_key_param
    pipeline_region     = var.pipeline_region
  })
}

# ----------------------------------------------------------------------------- secrets (placeholders)
# Set with: aws ssm put-parameter --overwrite --type SecureString --region <region> --name <name> --value ...
# (TG_SESSION: run `observer.py login --ssm` anywhere with AWS credentials, or via Session Manager on the instance)

resource "aws_ssm_parameter" "secret" {
  #checkov:skip=CKV_AWS_337:SecureString with the AWS-managed aws/ssm key; a CMK adds cost without a new access boundary beyond IAM
  for_each = toset(["api_id", "api_hash", "session"])
  name     = "${local.ssm_prefix}/${each.key}"
  type     = "SecureString"
  value    = "CHANGE_ME"
  lifecycle { ignore_changes = [value] }
}

# ----------------------------------------------------------------------------- IAM

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "observer" {
  statement {
    sid     = "ReadSecrets"
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:${local.region}:${local.account}:parameter${local.ssm_prefix}/*",
      "arn:aws:ssm:${var.pipeline_region}:${local.account}:parameter${var.collector_key_param}",
    ]
  }
  statement {
    sid       = "StoreSessionFromLogin"
    actions   = ["ssm:PutParameter"]
    resources = ["arn:aws:ssm:${local.region}:${local.account}:parameter${local.ssm_prefix}/session"]
  }
}

resource "aws_iam_role" "observer" {
  name               = "${var.name_prefix}-observer"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "observer" {
  name   = "${var.name_prefix}-observer"
  role   = aws_iam_role.observer.id
  policy = data.aws_iam_policy_document.observer.json
}

resource "aws_iam_role_policy_attachment" "session_manager" {
  role       = aws_iam_role.observer.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "observer" {
  name = "${var.name_prefix}-observer"
  role = aws_iam_role.observer.name
}

# ----------------------------------------------------------------------------- instance

resource "aws_security_group" "observer" {
  #checkov:skip=CKV_AWS_382:Outbound to Telegram's data centers, the collector URL, SSM and package mirrors; no inbound rules
  name        = "${var.name_prefix}-observer"
  description = "tv-tg-bot observer: outbound only"
  vpc_id      = data.aws_subnet.selected.vpc_id

  egress {
    description = "Telegram, collector, SSM, package mirrors"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "observer" {
  #checkov:skip=CKV_AWS_126:Detailed monitoring costs more than the instance's value; liveness is covered by the observer-missing alarm
  #checkov:skip=CKV_AWS_88:Public IPv4 is only for outbound access in default-VPC subnets (no inbound rules); set associate_public_ip = false with a NAT subnet
  ami                         = data.aws_ssm_parameter.ami.value
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnet.selected.id
  vpc_security_group_ids      = [aws_security_group.observer.id]
  iam_instance_profile        = aws_iam_instance_profile.observer.name
  associate_public_ip_address = var.associate_public_ip
  ebs_optimized               = true
  user_data_base64            = base64gzip(local.user_data) # raw user data is limited to 16 KB
  user_data_replace_on_change = true                        # observer code changes -> fresh instance

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  dynamic "credit_specification" {
    for_each = startswith(var.instance_type, "t") ? [1] : []
    content {
      cpu_credits = "standard" # never bill for burst; the observer is idle almost all the time
    }
  }

  lifecycle {
    ignore_changes = [ami] # don't replace the instance whenever a newer AL2023 AMI is published
  }

  tags = { Name = "${var.name_prefix}-observer" }

  depends_on = [aws_iam_role_policy.observer]
}

output "instance_id" {
  value = aws_instance.observer.id
}

output "region" {
  value = local.region
}

output "ssm_prefix" {
  value = local.ssm_prefix
}

output "shell_command" {
  description = "Open a shell on the instance (no SSH needed)."
  value       = "aws ssm start-session --region ${local.region} --target ${aws_instance.observer.id}"
}

output "user_data" {
  description = "Rendered cloud-init script (contains no secrets). Exposed for tests and debugging."
  value       = local.user_data
}
