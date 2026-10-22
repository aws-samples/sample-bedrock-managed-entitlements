# Both functions ship the same package as the CDK stack: the lambda/ directory
# as-is. boto3 comes from the Python runtime, so there is nothing to install.
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/.build/lambda.zip"
  excludes    = ["__pycache__", "steps/__pycache__", "requirements-dev.txt"]
}

# Log groups are managed here (rather than created on first invoke) so the
# retention applies from the start and terraform destroy removes them.
resource "aws_cloudwatch_log_group" "handler" {
  #checkov:skip=CKV_AWS_158:Matches the CDK stack, which uses the default CloudWatch Logs encryption.
  #checkov:skip=CKV_AWS_338:Retention mirrors the CDK stack (14 days) and is configurable via log_retention_days.
  name              = "/aws/lambda/${local.names.handler}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "handler" {
  #checkov:skip=CKV_AWS_50:X-Ray tracing is not enabled in the CDK stack this mirrors.
  #checkov:skip=CKV_AWS_115:Invoked only by low-volume EventBridge rules; no reserved concurrency, matching CDK.
  #checkov:skip=CKV_AWS_116:Matches the CDK stack; failures are reported through the SNS topic.
  #checkov:skip=CKV_AWS_117:Calls only public AWS APIs; no VPC resources to reach, matching CDK.
  #checkov:skip=CKV_AWS_173:Environment variables hold resource names and the organization ID, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not configured in the CDK stack this mirrors.
  function_name    = local.names.handler
  description      = "Creates and activates organization-wide License Manager grants for allow-listed Marketplace sellers"
  role             = aws_iam_role.handler.arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 300
  memory_size      = 256

  environment {
    variables = {
      SELLER_TABLE_NAME        = aws_dynamodb_table.allowed_sellers.name
      PENDING_GRANT_TABLE_NAME = aws_dynamodb_table.pending_grants.name
      SNS_TOPIC_ARN            = aws_sns_topic.notifications.arn
      ORGANIZATION_ID          = local.organization_id
      HOME_REGION              = "us-east-1"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.handler,
    aws_iam_role_policy.handler,
    terraform_data.config_checks,
  ]
}

# ─── Auto-accept handler (OPTIONAL — disabled by default) ───────────────────
# Accepts private offers from sellers that opted in with autoAcceptOffers and
# a sellerProfileId. See lambda/auto_accept_handler.py.
resource "aws_cloudwatch_log_group" "auto_accept" {
  #checkov:skip=CKV_AWS_158:Matches the CDK stack, which uses the default CloudWatch Logs encryption.
  #checkov:skip=CKV_AWS_338:Retention mirrors the CDK stack (14 days) and is configurable via log_retention_days.
  count = local.enable_auto_accept ? 1 : 0

  name              = "/aws/lambda/${local.names.auto_accept}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "auto_accept" {
  #checkov:skip=CKV_AWS_50:X-Ray tracing is not enabled in the CDK stack this mirrors.
  #checkov:skip=CKV_AWS_115:Invoked only by its schedule; no reserved concurrency, matching CDK.
  #checkov:skip=CKV_AWS_116:Matches the CDK stack; failures are reported through the SNS topic.
  #checkov:skip=CKV_AWS_117:Calls only public AWS APIs; no VPC resources to reach, matching CDK.
  #checkov:skip=CKV_AWS_173:Environment variables hold resource names, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not configured in the CDK stack this mirrors.
  count = local.enable_auto_accept ? 1 : 0

  function_name    = local.names.auto_accept
  description      = "Accepts private offers from opted-in allow-listed sellers"
  role             = aws_iam_role.auto_accept[0].arn
  runtime          = "python3.12"
  handler          = "auto_accept_handler.lambda_handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 300
  memory_size      = 256

  environment {
    variables = {
      SELLER_TABLE_NAME = aws_dynamodb_table.allowed_sellers.name
      SNS_TOPIC_ARN     = aws_sns_topic.notifications.arn
      HOME_REGION       = "us-east-1"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.auto_accept,
    aws_iam_role_policy.auto_accept,
  ]
}
