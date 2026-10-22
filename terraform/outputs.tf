output "lambda_function_arn" {
  description = "MPPO handler Lambda ARN"
  value       = aws_lambda_function.handler.arn
}

output "lambda_function_name" {
  description = "MPPO handler Lambda name (pass to scripts/simulate_event.py --function-name)"
  value       = aws_lambda_function.handler.function_name
}

output "seller_table_name" {
  description = "DynamoDB table for the seller allow-list (pass to scripts/seed_sellers.py --table-name)"
  value       = aws_dynamodb_table.allowed_sellers.name
}

output "pending_grant_table_name" {
  description = "DynamoDB table for pending grant activation retries"
  value       = aws_dynamodb_table.pending_grants.name
}

output "notification_topic_arn" {
  description = "SNS topic for notifications"
  value       = aws_sns_topic.notifications.arn
}

output "event_bridge_rule_name" {
  description = "EventBridge rule name"
  value       = aws_cloudwatch_event_rule.agreement_created.name
}

output "activation_retry_rule_name" {
  description = "EventBridge rule name for pending grant activation retries"
  value       = aws_cloudwatch_event_rule.activation_retry.name
}

output "auto_accept_lambda_arn" {
  description = "Auto-accept handler Lambda ARN (null unless auto-accept is enabled)"
  value       = one(aws_lambda_function.auto_accept[*].arn)
}

output "slack_channel_arn" {
  description = "AWS Chatbot Slack channel configuration ARN (null unless Slack is configured)"
  value       = one(aws_chatbot_slack_channel_configuration.notifications[*].chat_configuration_arn)
}
