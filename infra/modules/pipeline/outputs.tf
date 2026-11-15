output "ingest_url" {
  description = "Paste into the TradingView alert's Webhook URL."
  value       = aws_lambda_function_url.public["ingest"].function_url
}

output "collector_url" {
  description = "COLLECTOR_URL for the observer."
  value       = aws_lambda_function_url.public["collector"].function_url
}

output "function_names" {
  value = { for k, f in aws_lambda_function.fn : k => f.function_name }
}

output "log_group_names" {
  value = { for k, g in aws_cloudwatch_log_group.fn : k => g.name }
}

output "queue_name" {
  value = aws_sqs_queue.alerts.name
}

output "dlq_name" {
  value = aws_sqs_queue.dlq.name
}

output "timing_table" {
  value = aws_dynamodb_table.timing.name
}

output "ssm_parameters" {
  description = "Set real values with: aws ssm put-parameter --overwrite --type SecureString --name <name> --value <value>"
  value = {
    tv_secret     = aws_ssm_parameter.tv_secret.name
    bot_token     = aws_ssm_parameter.bot_token.name
    collector_key = aws_ssm_parameter.collector_key.name
    allowed_ips   = aws_ssm_parameter.allowed_ips.name
  }
}
