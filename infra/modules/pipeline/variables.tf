variable "name_prefix" {
  description = "Prefix for every resource name, e.g. tvtg-dev."
  type        = string
}

variable "src_dir" {
  description = "Path to the repo's src/ directory (zipped into the Lambda package)."
  type        = string
}

variable "chat_id" {
  description = "Telegram alert channel id (-100...)."
  type        = string
}

variable "canary_chat_id" {
  description = "Muted channel for canary alerts. Empty = canaries go to chat_id."
  type        = string
  default     = ""
}

variable "memory_mb" {
  description = "Memory for ingest and sender. CPU scales with memory, so this affects cold starts (Phase 3 experiment)."
  type        = number
  default     = 256
}

variable "ingest_reserved_concurrency" {
  description = "Caps the cost of abusive traffic on the public URL. -1 = unreserved (needed on new accounts whose concurrency quota is 10)."
  type        = number
  default     = -1
}

variable "allow_any_ip" {
  description = "Turn off the TradingView IP allowlist (dev only, for curl tests)."
  type        = bool
  default     = false
}

variable "tradingview_ips" {
  description = "Initial TradingView webhook IPs. Stored in SSM; later edits there take effect without a deploy."
  type        = list(string)
  default     = ["52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"]
}

variable "provisioned_concurrency_mode" {
  description = "off | always | market_hours (ingest + sender, on the live alias)."
  type        = string
  default     = "off"
  validation {
    condition     = contains(["off", "always", "market_hours"], var.provisioned_concurrency_mode)
    error_message = "provisioned_concurrency_mode must be off, always or market_hours."
  }
}

variable "market_open_cron" {
  description = "When market_hours provisioned concurrency switches on (EventBridge Scheduler cron, in market_timezone)."
  type        = string
  default     = "cron(20 9 ? * MON-FRI *)"
}

variable "market_close_cron" {
  description = "When market_hours provisioned concurrency switches off."
  type        = string
  default     = "cron(5 16 ? * MON-FRI *)"
}

variable "market_timezone" {
  type    = string
  default = "America/New_York"
}

variable "keepalive_minutes" {
  description = "Ping ingest and sender every N minutes to keep containers and the Telegram connection warm. 0 = off."
  type        = number
  default     = 0
}

variable "log_retention_days" {
  type    = number
  default = 90
}

variable "secrets_revision" {
  description = "Bump after rotating an SSM secret to force fresh containers (secrets are cached per container)."
  type        = string
  default     = "1"
}
