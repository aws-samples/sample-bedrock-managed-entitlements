# ─── Marketplace agreement created ──────────────────────────────────────────
# Source: aws.agreement-marketplace, delivered to the us-east-1 default bus.
resource "aws_cloudwatch_event_rule" "agreement_created" {
  name        = local.names.agreement_rule
  description = "Captures Marketplace purchase agreement events for MPPO auto-grant distribution"
  state       = var.agreement_rule_enabled ? "ENABLED" : "DISABLED"

  event_pattern = jsonencode({
    source        = ["aws.agreement-marketplace"]
    "detail-type" = ["Purchase Agreement Created - Acceptor"]
  })
}

resource "aws_cloudwatch_event_target" "agreement_created" {
  rule = aws_cloudwatch_event_rule.agreement_created.name
  arn  = aws_lambda_function.handler.arn
}

resource "aws_lambda_permission" "agreement_created" {
  statement_id  = "AllowAgreementCreatedRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.handler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.agreement_created.arn
}

# ─── Pending grant activation retry ─────────────────────────────────────────
# The handler recognises this payload and re-checks every pending grant.
resource "aws_cloudwatch_event_rule" "activation_retry" {
  name                = local.names.retry_rule
  description         = "Retries License Manager grant activation for pending MPPO grants"
  schedule_expression = var.activation_retry_schedule
}

resource "aws_cloudwatch_event_target" "activation_retry" {
  rule = aws_cloudwatch_event_rule.activation_retry.name
  arn  = aws_lambda_function.handler.arn

  input = jsonencode({
    source        = "mppo-grants-automation"
    "detail-type" = "Retry Pending MPPO Grant Activations"
    detail        = {}
  })
}

resource "aws_lambda_permission" "activation_retry" {
  statement_id  = "AllowActivationRetryRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.handler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.activation_retry.arn
}

# ─── Entitlement reconciliation ─────────────────────────────────────────────
resource "aws_cloudwatch_event_rule" "entitlement_monitor" {
  name                = local.names.monitor_rule
  description         = "Reconciles unaccepted offers and inactive or missing License Manager grants"
  schedule_expression = local.monitor_schedule
}

resource "aws_cloudwatch_event_target" "entitlement_monitor" {
  rule = aws_cloudwatch_event_rule.entitlement_monitor.name
  arn  = aws_lambda_function.handler.arn

  input = jsonencode({
    source        = "mppo-grants-automation"
    "detail-type" = "Monitor MPPO Entitlements"
    detail        = {}
  })
}

resource "aws_lambda_permission" "entitlement_monitor" {
  statement_id  = "AllowEntitlementMonitorRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.handler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.entitlement_monitor.arn
}

# ─── Auto-accept schedule (opt-in) ──────────────────────────────────────────
resource "aws_cloudwatch_event_rule" "auto_accept" {
  count = local.enable_auto_accept ? 1 : 0

  name                = local.names.auto_accept_rule
  description         = "Periodically checks for pending offers to auto-accept (opt-in)"
  schedule_expression = local.auto_accept_schedule
}

resource "aws_cloudwatch_event_target" "auto_accept" {
  count = local.enable_auto_accept ? 1 : 0

  rule = aws_cloudwatch_event_rule.auto_accept[0].name
  arn  = aws_lambda_function.auto_accept[0].arn
}

resource "aws_lambda_permission" "auto_accept" {
  count = local.enable_auto_accept ? 1 : 0

  statement_id  = "AllowAutoAcceptSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auto_accept[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.auto_accept[0].arn
}
