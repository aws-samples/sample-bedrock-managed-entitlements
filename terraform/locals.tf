locals {
  # Same file the CDK stack, setup_config.py, and seed_sellers.py use, so one
  # config drives either deployment path. Missing file => variables only.
  config_path = coalesce(var.config_path, "${path.module}/../config/sellers.json")
  config      = jsondecode(fileexists(local.config_path) ? file(local.config_path) : "{}")

  config_notifications = try(local.config.notifications, {})

  organization_id = coalesce(var.organization_id, try(local.config.organizationId, ""), "unset")

  # Skip the placeholder address from sellers.example.json, matching CDK.
  notification_emails = toset([
    for email in coalesce(var.notification_emails, try(local.config_notifications.emailAddresses, []), []) :
    email if email != "admin@example.com"
  ])

  slack_workspace_id = coalesce(var.slack_workspace_id, try(local.config_notifications.slackWorkspaceId, ""), "none")
  slack_channel_id   = coalesce(var.slack_channel_id, try(local.config_notifications.slackChannelId, ""), "none")
  enable_slack       = local.slack_workspace_id != "none" && local.slack_channel_id != "none"

  enable_auto_accept   = coalesce(var.enable_auto_accept, try(local.config.enableAutoAccept, false), false)
  auto_accept_schedule = coalesce(var.auto_accept_schedule, try(local.config.autoAcceptSchedule, ""), "rate(1 hour)")
  monitor_schedule     = coalesce(var.monitor_schedule, try(local.config.monitorSchedule, ""), "rate(1 day)")
  notification_mode    = coalesce(var.notification_mode, try(local.config_notifications.mode, ""), "exceptions_only")

  names = {
    seller_table        = "${var.name_prefix}-allowed-sellers"
    pending_table       = "${var.name_prefix}-pending-grants"
    topic               = "${var.name_prefix}-grants-notifications"
    handler             = "${var.name_prefix}-grants-handler"
    auto_accept         = "${var.name_prefix}-auto-accept-handler"
    agreement_rule      = "${var.name_prefix}-agreement-created"
    retry_rule          = "${var.name_prefix}-grant-activation-retry"
    auto_accept_rule    = "${var.name_prefix}-auto-accept-schedule"
    monitor_rule        = "${var.name_prefix}-entitlement-monitor"
    slack_configuration = "${var.name_prefix}-grants-notifications"
  }

  partition = data.aws_partition.current.partition
}

data "aws_partition" "current" {}

# Fail at plan time, before anything is created, when required config is missing.
resource "terraform_data" "config_checks" {
  input = local.organization_id

  lifecycle {
    precondition {
      condition     = can(regex("^o-[a-z0-9]{10,32}$", local.organization_id))
      error_message = "Set organization_id (or organizationId in ${local.config_path}) to your AWS Organizations ID, for example o-abc1234567. Run scripts/setup_config.py to generate the config file."
    }

    precondition {
      condition     = (local.slack_workspace_id == "none") == (local.slack_channel_id == "none")
      error_message = "Set slack_workspace_id and slack_channel_id together, or neither."
    }
  }
}
