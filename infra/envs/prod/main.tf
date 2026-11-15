provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "tv-tg-bot", env = "prod", managed_by = "terraform" }
  }
}

module "pipeline" {
  source  = "../../modules/pipeline"
  src_dir = "${path.root}/../../../src"

  name_prefix    = "tvtg-prod"
  chat_id        = var.chat_id
  canary_chat_id = var.canary_chat_id

  allow_any_ip                 = false
  ingest_reserved_concurrency  = 5        # needs an account concurrency quota above 10 (docs/deploy.md)
  provisioned_concurrency_mode = "always" # crypto trades 24/7; "market_hours" for equities only
  keepalive_minutes            = 3        # keeps the Telegram connection warm (Phase 3 experiment)
}

module "observability" {
  source          = "../../modules/observability"
  name_prefix     = "tvtg-prod"
  function_names  = module.pipeline.function_names
  log_group_names = module.pipeline.log_group_names
  dlq_name        = module.pipeline.dlq_name
  alarm_email     = var.alarm_email
  canary_enabled  = true

  observer_expected = var.observer_host != "none"
}

# ----------------------------------------------------------------------------- reception observer

provider "aws" {
  alias  = "observer"
  region = coalesce(var.observer_region, var.region)
  default_tags {
    tags = { project = "tv-tg-bot", env = "prod", managed_by = "terraform" }
  }
}

module "observer_ec2" {
  count     = var.observer_host == "ec2" ? 1 : 0
  source    = "../../modules/observer_ec2"
  providers = { aws = aws.observer }

  name_prefix         = "tvtg-prod"
  instance_type       = var.observer_instance_type
  architecture        = var.observer_architecture
  chat_ids            = compact([var.chat_id, var.canary_chat_id])
  collector_url       = module.pipeline.collector_url
  collector_key_param = module.pipeline.ssm_parameters["collector_key"]
  pipeline_region     = var.region
}
