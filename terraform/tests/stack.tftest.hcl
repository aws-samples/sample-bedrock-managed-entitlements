# Plan-only checks that mirror tests/test_cdk_stack.py. They run offline with a
# mocked AWS provider:  terraform init && terraform test

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
}

# Point at a file that does not exist so a local config/sellers.json never
# leaks into the tests, and pin the remaining inputs so TF_VAR_* values in the
# caller's shell do not either.
variables {
  config_path            = "tests/no-such-config.json"
  organization_id        = "o-abc1234567"
  name_prefix            = "mppo"
  notification_emails    = null
  slack_workspace_id     = null
  slack_channel_id       = null
  enable_auto_accept     = null
  auto_accept_schedule   = null
  monitor_schedule       = null
  notification_mode      = null
  agreement_rule_enabled = true
}

run "defaults_match_cdk_names" {
  command = plan

  assert {
    condition     = aws_lambda_function.handler.function_name == "mppo-grants-handler"
    error_message = "Handler name should match the CDK stack."
  }

  assert {
    condition     = aws_dynamodb_table.allowed_sellers.name == "mppo-allowed-sellers" && aws_dynamodb_table.allowed_sellers.hash_key == "proposerAccountId"
    error_message = "Seller table name or key does not match the CDK stack."
  }

  assert {
    condition     = aws_dynamodb_table.pending_grants.name == "mppo-pending-grants" && aws_dynamodb_table.pending_grants.hash_key == "grantArn"
    error_message = "Pending grant table name or key does not match the CDK stack."
  }

  assert {
    condition     = aws_sns_topic.notifications.name == "mppo-grants-notifications"
    error_message = "Topic name should match the CDK stack."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.agreement_created.name == "mppo-agreement-created" && aws_cloudwatch_event_rule.activation_retry.name == "mppo-grant-activation-retry" && aws_cloudwatch_event_rule.entitlement_monitor.name == "mppo-entitlement-monitor"
    error_message = "Rule names should match the CDK stack."
  }
}

run "handler_configuration" {
  command = plan

  assert {
    condition = (
      aws_lambda_function.handler.runtime == "python3.12" &&
      aws_lambda_function.handler.handler == "handler.lambda_handler" &&
      aws_lambda_function.handler.timeout == 300 &&
      aws_lambda_function.handler.memory_size == 256
    )
    error_message = "Handler runtime settings should match the CDK stack."
  }

  assert {
    condition = (
      aws_lambda_function.handler.environment[0].variables["ORGANIZATION_ID"] == "o-abc1234567" &&
      aws_lambda_function.handler.environment[0].variables["SELLER_TABLE_NAME"] == "mppo-allowed-sellers" &&
      aws_lambda_function.handler.environment[0].variables["PENDING_GRANT_TABLE_NAME"] == "mppo-pending-grants" &&
      aws_lambda_function.handler.environment[0].variables["HOME_REGION"] == "us-east-1" &&
      aws_lambda_function.handler.environment[0].variables["NOTIFICATION_MODE"] == "exceptions_only" &&
      aws_lambda_function.handler.environment[0].variables["DEPLOYMENT_NAME"] == "mppo"
    )
    error_message = "Handler environment does not match the CDK stack."
  }

  assert {
    condition     = aws_cloudwatch_log_group.handler.retention_in_days == 14
    error_message = "Log retention should default to 14 days like the CDK stack."
  }
}

run "eventbridge_rules" {
  command = plan

  assert {
    condition = jsondecode(aws_cloudwatch_event_rule.agreement_created.event_pattern) == {
      source        = ["aws.agreement-marketplace"]
      "detail-type" = ["Purchase Agreement Created - Acceptor"]
    }
    error_message = "Agreement rule must match Marketplace agreement created events only."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.agreement_created.state == "ENABLED"
    error_message = "Agreement rule should be enabled by default."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.activation_retry.schedule_expression == "rate(6 hours)"
    error_message = "Retry rule should run every 6 hours like the CDK stack."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.entitlement_monitor.schedule_expression == "rate(1 day)"
    error_message = "Entitlement reconciliation should run daily by default."
  }

  assert {
    condition = jsondecode(aws_cloudwatch_event_target.entitlement_monitor.input) == {
      source        = "mppo-grants-automation"
      "detail-type" = "Monitor MPPO Entitlements"
      detail        = {}
    }
    error_message = "Monitor target input must match the payload lambda/handler.py recognises."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.handler_errors.treat_missing_data == "notBreaching"
    error_message = "Handler failures should alarm without treating idle periods as failures."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.monitor_heartbeat_missing.metric_name == "ReconciliationCompleted" &&
      aws_cloudwatch_metric_alarm.monitor_heartbeat_missing.evaluation_periods == 2 &&
      aws_cloudwatch_metric_alarm.monitor_heartbeat_missing.datapoints_to_alarm == 2 &&
      aws_cloudwatch_metric_alarm.monitor_heartbeat_missing.treat_missing_data == "breaching"
    )
    error_message = "A dead-man alarm should detect when daily reconciliation stops completing."
  }

  assert {
    condition = jsondecode(aws_cloudwatch_event_target.activation_retry.input) == {
      source        = "mppo-grants-automation"
      "detail-type" = "Retry Pending MPPO Grant Activations"
      detail        = {}
    }
    error_message = "Retry target input must match the payload lambda/handler.py recognises."
  }
}

run "auto_accept_disabled_by_default" {
  command = plan

  assert {
    condition     = length(aws_lambda_function.auto_accept) == 0 && length(aws_cloudwatch_event_rule.auto_accept) == 0 && length(aws_iam_role.auto_accept) == 0
    error_message = "Auto-accept resources must not be created unless enabled."
  }
}

run "auto_accept_enabled" {
  command = plan

  variables {
    enable_auto_accept   = true
    auto_accept_schedule = "rate(2 hours)"
  }

  assert {
    condition     = aws_lambda_function.auto_accept[0].function_name == "mppo-auto-accept-handler" && aws_lambda_function.auto_accept[0].handler == "auto_accept_handler.lambda_handler"
    error_message = "Auto-accept handler should match the CDK stack."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.auto_accept[0].schedule_expression == "rate(2 hours)"
    error_message = "Auto-accept schedule should come from auto_accept_schedule."
  }
}

run "name_prefix_applies_everywhere" {
  command = plan

  variables {
    name_prefix            = "mppo-tf"
    agreement_rule_enabled = false
  }

  assert {
    condition = alltrue([
      aws_lambda_function.handler.function_name == "mppo-tf-grants-handler",
      aws_iam_role.handler.name == "mppo-tf-grants-handler",
      aws_dynamodb_table.allowed_sellers.name == "mppo-tf-allowed-sellers",
      aws_dynamodb_table.pending_grants.name == "mppo-tf-pending-grants",
      aws_sns_topic.notifications.name == "mppo-tf-grants-notifications",
      aws_cloudwatch_event_rule.agreement_created.name == "mppo-tf-agreement-created",
      aws_cloudwatch_event_rule.activation_retry.name == "mppo-tf-grant-activation-retry",
      aws_cloudwatch_event_rule.entitlement_monitor.name == "mppo-tf-entitlement-monitor",
      aws_cloudwatch_log_group.handler.name == "/aws/lambda/mppo-tf-grants-handler",
    ])
    error_message = "Every resource name should use name_prefix."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.agreement_created.state == "DISABLED"
    error_message = "agreement_rule_enabled = false should disable the agreement rule."
  }
}

run "placeholder_email_is_skipped" {
  command = plan

  variables {
    notification_emails = ["admin@example.com", "ops@example.org"]
  }

  assert {
    condition     = keys(aws_sns_topic_subscription.email) == ["ops@example.org"]
    error_message = "The sellers.example.json placeholder address should not be subscribed."
  }
}

run "missing_organization_id_fails" {
  command = plan

  variables {
    organization_id = null
  }

  expect_failures = [terraform_data.config_checks]
}

run "slack_ids_must_be_set_together" {
  command = plan

  variables {
    slack_workspace_id = "T01ABCDEF"
  }

  expect_failures = [terraform_data.config_checks]
}
