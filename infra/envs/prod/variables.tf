variable "region" {
  type    = string
  default = "us-west-2" # TradingView's webhook IPs look like AWS us-west-2 (docs/design.md D4)
}

variable "chat_id" {
  description = "Telegram alert channel id (-100...)."
  type        = string
}

variable "canary_chat_id" {
  description = "Muted canary channel id. Empty = canaries go to chat_id."
  type        = string
  default     = ""
}

variable "alarm_email" {
  type    = string
  default = ""
}

# ----------------------------------------------------------------------------- reception observer
variable "observer_host" {
  description = <<-EOT
    Where the reception observer runs (docs/deploy.md step 8):
      "none"     - no observer (reception panels stay empty)
      "external" - a machine you run: Raspberry Pi / any Linux box (observer/install.sh) or Docker (observer/compose.yaml)
      "ec2"      - Terraform runs it on a small EC2 instance (observer_instance_type, in observer_region)
  EOT
  type        = string
  default     = "none"
  validation {
    condition     = contains(["none", "external", "ec2"], var.observer_host)
    error_message = "observer_host must be none, external or ec2."
  }
}

variable "observer_region" {
  description = "Region for the ec2 observer. Pick the one nearest you; empty = same as region."
  type        = string
  default     = ""
}

variable "observer_instance_type" {
  type    = string
  default = "t4g.nano"
}

variable "observer_architecture" {
  description = "arm64 for Graviton (t4g), x86_64 for t3/t3a."
  type        = string
  default     = "arm64"
}
