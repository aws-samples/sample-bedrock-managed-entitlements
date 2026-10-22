resource "aws_sns_topic" "notifications" {
  name              = local.names.topic
  display_name      = "MPPO Grants Automation Notifications"
  kms_master_key_id = var.sns_kms_master_key_id
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
