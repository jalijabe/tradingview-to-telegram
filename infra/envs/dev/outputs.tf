output "ingest_url" {
  value = module.pipeline.ingest_url
}

output "collector_url" {
  value = module.pipeline.collector_url
}

output "ssm_parameters" {
  value = module.pipeline.ssm_parameters
}

output "dashboard" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${module.observability.dashboard_name}"
}

output "observer" {
  value = {
    host          = var.observer_host
    instance_id   = try(module.observer_ec2[0].instance_id, null)
    ssm_prefix    = try(module.observer_ec2[0].ssm_prefix, null)
    shell_command = try(module.observer_ec2[0].shell_command, null)
  }
}
