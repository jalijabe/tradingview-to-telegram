terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
}

variable "name_prefix" {
  type = string
}

variable "namespace" {
  description = "Must match tvtg.emf.NAMESPACE."
  type        = string
  default     = "TvTgBot"
}

variable "function_names" {
  description = "Map with keys ingest, sender, collector."
  type        = map(string)
}

variable "log_group_names" {
  description = "Map with keys ingest, sender, collector."
  type        = map(string)
}

variable "dlq_name" {
  type = string
}

variable "alarm_email" {
  description = "Where alarm notifications go. Empty = SNS topic only."
  type        = string
  default     = ""
}

variable "end_to_end_p90_threshold_ms" {
  type    = number
  default = 3000
}

variable "canary_enabled" {
  description = "Alarm when no alert has been delivered for canary_missing_minutes (needs the 1-minute canary alert)."
  type        = bool
  default     = false
}

variable "observer_expected" {
  description = "Alarm when no observer report has arrived for observer_missing_minutes (any host type)."
  type        = bool
  default     = false
}

variable "observer_missing_minutes" {
  type    = number
  default = 30
}

variable "canary_missing_minutes" {
  type    = number
  default = 10
}

data "aws_region" "current" {}

locals {
  region = data.aws_region.current.region
  lg     = var.log_group_names
}

# ----------------------------------------------------------------------------- notifications

resource "aws_sns_topic" "alarms" {
  #checkov:skip=CKV_AWS_26:CloudWatch alarms cannot publish to a topic encrypted with the aws/sns managed key; a CMK costs more than the stack and alarm text is not sensitive
  name = "${var.name_prefix}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ----------------------------------------------------------------------------- log-derived counters

resource "aws_cloudwatch_log_metric_filter" "rejected" {
  name           = "${var.name_prefix}-rejected"
  log_group_name = local.lg["ingest"]
  pattern        = "{ $.event = \"rejected\" }"
  metric_transformation {
    name          = "rejected"
    namespace     = var.namespace
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "dropped" {
  name           = "${var.name_prefix}-dropped"
  log_group_name = local.lg["sender"]
  pattern        = "{ $.event = \"dropped\" }"
  metric_transformation {
    name          = "dropped"
    namespace     = var.namespace
    value         = "1"
    default_value = "0"
  }
}

# ----------------------------------------------------------------------------- alarms

resource "aws_cloudwatch_metric_alarm" "dlq" {
  alarm_name          = "${var.name_prefix}-dlq-not-empty"
  alarm_description   = "Alerts failed delivery 3 times. Inspect, fix, then redrive the DLQ."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = var.dlq_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  for_each            = var.function_names
  alarm_name          = "${var.name_prefix}-${each.key}-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "slow" {
  alarm_name          = "${var.name_prefix}-end-to-end-p90"
  alarm_description   = "p90 TradingView-fired -> Telegram-accepted latency is high."
  namespace           = var.namespace
  metric_name         = "end_to_end"
  extended_statistic  = "p90"
  period              = 900
  evaluation_periods  = 2
  threshold           = var.end_to_end_p90_threshold_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "rejected_spike" {
  alarm_name          = "${var.name_prefix}-rejected-spike"
  alarm_description   = "Many rejected webhooks: probing, a wrong secret, or TradingView changed its IPs (check source_ip in the ingest logs)."
  namespace           = var.namespace
  metric_name         = "rejected"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 20
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "dropped" {
  alarm_name          = "${var.name_prefix}-dropped"
  alarm_description   = "Telegram permanently rejected an alert (bad chat id, bot removed, formatting)."
  namespace           = var.namespace
  metric_name         = "dropped"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "canary_missing" {
  count               = var.canary_enabled ? 1 : 0
  alarm_name          = "${var.name_prefix}-canary-missing"
  alarm_description   = "Nothing delivered recently: TradingView, AWS or Telegram is down, or the canary alert expired."
  namespace           = var.namespace
  metric_name         = "end_to_end"
  statistic           = "SampleCount"
  period              = var.canary_missing_minutes * 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "observer_missing" {
  count               = var.observer_expected ? 1 : 0
  alarm_name          = "${var.name_prefix}-observer-missing"
  alarm_description   = "No reception reports: the observer host is down, logged out, or its secrets aren't set (check: observer.py check)."
  namespace           = var.namespace
  metric_name         = "tg_to_client"
  statistic           = "SampleCount"
  period              = var.observer_missing_minutes * 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# ----------------------------------------------------------------------------- dashboard

locals {
  delivered_src = "SOURCE '${local.lg["sender"]}' | filter event = \"delivered\" and receive_count <= 1"

  metric_widget = {
    for name, title in {
      end_to_end   = "End to end: TradingView fired → Telegram accepted (t8 − t1)"
      aws_total    = "Inside AWS (t8 − t2)"
      tv_to_client = "TradingView fired → client received (t9 − t1)"
      tg_to_client = "Telegram request → client received (t9 − t7)"
      } : name => {
      type   = "metric"
      width  = 12
      height = 6
      properties = {
        title  = title
        region = local.region
        view   = "timeSeries"
        period = 3600
        stat   = "p50"
        yAxis  = { left = { min = 0, label = "ms", showUnits = false } }
        metrics = [
          [var.namespace, name, { stat = "p50", label = "p50" }],
          [var.namespace, name, { stat = "p90", label = "p90" }],
          [var.namespace, name, { stat = "p99", label = "p99" }],
        ]
      }
    }
  }

  log_widgets = [
    {
      type = "log", width = 24, height = 7
      properties = {
        title   = "Median per segment (ms), hourly"
        region  = local.region
        view    = "timeSeries"
        stacked = true
        query   = "${local.delivered_src} | stats pct(fire_to_edge, 50) as fire_to_edge, pct(edge_to_ingest, 50) as edge_to_ingest, pct(ingest_to_queue, 50) as ingest_to_queue, pct(queue_wait, 50) as queue_wait, pct(dequeue_to_sender, 50) as dequeue_to_sender, pct(sender_prep, 50) as sender_prep, pct(telegram_rtt, 50) as telegram_rtt by bin(1h)"
      }
    },
    {
      type = "log", width = 12, height = 6
      properties = {
        title  = "Cold starts and new Telegram connections vs latency"
        region = local.region
        view   = "table"
        query  = "${local.delivered_src} | stats count(*) as alerts, pct(aws_total, 50) as p50_ms, pct(aws_total, 99) as p99_ms by ingest_cold, sender_cold, tg_new_conn"
      }
    },
    {
      type = "log", width = 12, height = 6
      properties = {
        title  = "Problems (last period)"
        region = local.region
        view   = "table"
        query  = "SOURCE '${local.lg["ingest"]}' | SOURCE '${local.lg["sender"]}' | filter event in [\"rejected\", \"dropped\", \"send_failed\", \"rate_limited\", \"ingest_failed\", \"post_send_failed\"] | stats count(*) as n by event, reason"
      }
    },
    {
      type = "log", width = 24, height = 5
      properties = {
        title  = "Reception by observer host (ms)"
        region = local.region
        view   = "table"
        query  = "SOURCE '${local.lg["collector"]}' | filter event = \"received\" | stats count(*) as reports, pct(tg_to_client, 50) as tg_to_client_p50, pct(tg_to_client, 90) as tg_to_client_p90, max(t9) as last_report_ms by observer"
      }
    },
    {
      type = "log", width = 24, height = 8
      properties = {
        title  = "Latest 50 alerts (ms)"
        region = local.region
        view   = "table"
        query  = "SOURCE '${local.lg["sender"]}' | filter event = \"delivered\" | sort @timestamp desc | limit 50 | fields @timestamp, ticker, canary, end_to_end, fire_to_edge, edge_to_ingest, ingest_to_queue, queue_wait, dequeue_to_sender, telegram_rtt, ingest_cold, sender_cold, tg_new_conn, receive_count"
      }
    },
  ]

  health_widget = {
    type = "metric", width = 24, height = 5
    properties = {
      title  = "Health"
      region = local.region
      view   = "timeSeries"
      period = 300
      metrics = concat(
        [["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dlq_name, { stat = "Maximum", label = "DLQ depth" }]],
        [for k, fn in var.function_names : ["AWS/Lambda", "Errors", "FunctionName", fn, { stat = "Sum", label = "${k} errors" }]],
        [[var.namespace, "rejected", { stat = "Sum", label = "rejected webhooks" }],
        [var.namespace, "dropped", { stat = "Sum", label = "dropped alerts" }]],
      )
    }
  }
}

resource "aws_cloudwatch_dashboard" "latency" {
  dashboard_name = "${var.name_prefix}-latency"
  dashboard_body = jsonencode({
    widgets = concat(
      [local.metric_widget["end_to_end"], local.metric_widget["aws_total"]],
      local.log_widgets,
      [local.metric_widget["tv_to_client"], local.metric_widget["tg_to_client"]],
      [local.health_widget],
    )
  })
}

output "alarm_topic_arn" {
  value = aws_sns_topic.alarms.arn
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.latency.dashboard_name
}
