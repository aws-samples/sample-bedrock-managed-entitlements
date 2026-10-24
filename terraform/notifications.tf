resource "aws_sns_topic" "notifications" {
  name              = local.names.topic
  display_name      = "MPPO Grants Automation Notifications"
  kms_master_key_id = var.sns_kms_master_key_id
}

resource "aws_cloudwatch_metric_alarm" "handler_errors" {
  alarm_name          = "${local.names.handler}-errors"
  alarm_description   = "The MPPO grant handler or entitlement monitor failed"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    FunctionName = aws_lambda_function.handler.function_name
  }
  alarm_actions = [aws_sns_topic.notifications.arn]
}

resource "aws_cloudwatch_metric_alarm" "monitor_heartbeat_missing" {
  alarm_name          = "${local.names.handler}-monitor-heartbeat-missing"
  alarm_description   = "The scheduled MPPO entitlement reconciliation has not completed for two daily periods"
  namespace           = "MPPO/Entitlements"
  metric_name         = "ReconciliationCompleted"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  dimensions = {
    Deployment = var.name_prefix
  }
  alarm_actions = [aws_sns_topic.notifications.arn]
}

resource "aws_cloudwatch_metric_alarm" "auto_accept_errors" {
  count = local.enable_auto_accept ? 1 : 0

  alarm_name          = "${local.names.auto_accept}-errors"
  alarm_description   = "The MPPO private-offer auto-accept handler failed"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    FunctionName = aws_lambda_function.auto_accept[0].function_name
  }
  alarm_actions = [aws_sns_topic.notifications.arn]
}

resource "aws_sns_topic_subscription" "email" {
  for_each = local.notification_emails

  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = each.value
}

# ─── AWS Chatbot: Slack (optional) ──────────────────────────────────────────
# Authorize the AWS Chatbot app in your Slack workspace once before applying.
resource "aws_iam_role" "chatbot" {
  count = local.enable_slack ? 1 : 0

  name               = "${var.name_prefix}-chatbot-slack"
  assume_role_policy = data.aws_iam_policy_document.chatbot_assume.json
}

data "aws_iam_policy_document" "chatbot_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["chatbot.amazonaws.com"]
    }
  }
}

resource "aws_chatbot_slack_channel_configuration" "notifications" {
  count = local.enable_slack ? 1 : 0

  configuration_name = local.names.slack_configuration
  slack_team_id      = local.slack_workspace_id
  slack_channel_id   = local.slack_channel_id
  iam_role_arn       = aws_iam_role.chatbot[0].arn
  sns_topic_arns     = [aws_sns_topic.notifications.arn]

  # Notification-only: the channel role has no policies, so Slack users cannot
  # run AWS commands through Chatbot.
  guardrail_policy_arns = ["arn:${local.partition}:iam::aws:policy/ReadOnlyAccess"]
}
