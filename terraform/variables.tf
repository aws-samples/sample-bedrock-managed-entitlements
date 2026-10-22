variable "config_path" {
  description = "Path to the sellers config JSON shared with the CDK stack and scripts. Values set in the variables below take precedence over the file. Leave the file absent to configure everything through variables."
  type        = string
  default     = null
}

variable "organization_id" {
  description = "AWS Organizations ID that receives the grants (o-xxxxxxxxxx). Falls back to organizationId in config_path."
  type        = string
  default     = null

  validation {
    condition     = var.organization_id == null || can(regex("^o-[a-z0-9]{10,32}$", var.organization_id))
    error_message = "organization_id must look like o-xxxxxxxxxx."
  }
}

variable "name_prefix" {
  description = "Prefix for every named resource. The default matches the CDK stack and the helper scripts' defaults; change it to run a second copy side by side."
  type        = string
  default     = "mppo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name_prefix))
    error_message = "name_prefix must be 2-21 characters: lowercase letters, digits, and hyphens, starting with a letter."
  }
}

variable "notification_emails" {
  description = "Email addresses subscribed to the notification topic. Falls back to notifications.emailAddresses in config_path. Each address must confirm the subscription."
  type        = list(string)
  default     = null
}

variable "slack_workspace_id" {
  description = "AWS Chatbot Slack workspace ID (T...). Set together with slack_channel_id to post notifications to Slack. Falls back to config_path."
  type        = string
  default     = null
}

variable "slack_channel_id" {
  description = "Slack channel ID (C...) for AWS Chatbot notifications. Falls back to config_path."
  type        = string
  default     = null
}

variable "enable_auto_accept" {
  description = "Deploy the optional auto-accept Lambda. Accepting offers creates financial commitments; each seller must also opt in with autoAcceptOffers and sellerProfileId. Falls back to enableAutoAccept in config_path, then false."
  type        = bool
  default     = null
}

variable "auto_accept_schedule" {
  description = "EventBridge schedule expression for the auto-accept Lambda. Falls back to autoAcceptSchedule in config_path, then rate(1 hour)."
  type        = string
  default     = null
}

variable "activation_retry_schedule" {
  description = "EventBridge schedule expression for retrying grants that License Manager has not finished processing."
  type        = string
  default     = "rate(6 hours)"
}

variable "agreement_rule_enabled" {
  description = "Whether the Marketplace agreement rule is ENABLED. Set false while a second deployment (for example the CDK stack) handles the same events, so one agreement is not processed twice."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda log groups."
  type        = number
  default     = 14
}

variable "sns_kms_master_key_id" {
  description = "Optional KMS key for SNS server-side encryption. AWS Chatbot requires a customer managed key whose policy allows chatbot.amazonaws.com; the AWS managed alias/aws/sns key does not work with Chatbot."
  type        = string
  default     = null
}

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}
}
