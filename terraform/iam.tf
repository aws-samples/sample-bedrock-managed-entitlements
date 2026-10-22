# Execution roles mirror the CDK stack's grants. License Manager, Marketplace,
# Organizations, and STS actions do not support resource-level permissions, so
# those statements use "*" exactly as the CDK stack does. Logging is scoped to
# each function's own log group instead of AWSLambdaBasicExecutionRole.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  # Same action sets as CDK grant_read_data / grant_read_write_data. The
  # tables have no streams, so the stream read actions are left out.
  dynamodb_read_actions = [
    "dynamodb:BatchGetItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
  ]

  dynamodb_write_actions = [
    "dynamodb:BatchWriteItem",
    "dynamodb:DeleteItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
  ]
}

# ─── Grant handler ──────────────────────────────────────────────────────────
resource "aws_iam_role" "handler" {
  name               = local.names.handler
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "handler" {
  #checkov:skip=CKV_AWS_356:Grant and license ARNs are created by AWS Marketplace and unknown in advance; matches the CDK stack.
  #checkov:skip=CKV_AWS_111:CreateGrant/CreateGrantVersion/AcceptGrant must act on licenses issued after deployment; matches the CDK stack.
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.handler.arn}:*"]
  }

  statement {
    sid       = "SellerAllowListRead"
    actions   = local.dynamodb_read_actions
    resources = [aws_dynamodb_table.allowed_sellers.arn]
  }

  statement {
    sid       = "PendingGrantsReadWrite"
    actions   = concat(local.dynamodb_read_actions, local.dynamodb_write_actions)
    resources = [aws_dynamodb_table.pending_grants.arn]
  }

  statement {
    sid       = "PublishNotifications"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.notifications.arn]
  }

  statement {
    sid = "LicenseManagerReadWrite"
    actions = [
      "license-manager:AcceptGrant",
      "license-manager:CreateGrant",
      "license-manager:CreateGrantVersion",
      "license-manager:GetGrant",
      "license-manager:GetLicense",
      "license-manager:GetLicenseUsage",
      "license-manager:ListDistributedGrants",
      "license-manager:ListReceivedLicenses",
    ]
    resources = ["*"]
  }

  statement {
    sid = "MarketplaceAgreementRead"
    actions = [
      "aws-marketplace:DescribeAgreement",
      "aws-marketplace:GetAgreementTerms",
      "aws-marketplace:SearchAgreements",
    ]
    resources = ["*"]
  }

  statement {
    sid = "MarketplaceDiscoveryRead"
    actions = [
      "aws-marketplace:GetOffer",
      "aws-marketplace:ListPurchaseOptions",
    ]
    resources = ["*"]
  }

  statement {
    sid = "OrganizationsRead"
    actions = [
      "organizations:DescribeOrganization",
      "organizations:ListAccounts",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "STSGetCaller"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }

  statement {
    sid       = "PublishMonitorHeartbeat"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["MPPO/Entitlements"]
    }
  }
}

resource "aws_iam_role_policy" "handler" {
  name   = local.names.handler
  role   = aws_iam_role.handler.id
  policy = data.aws_iam_policy_document.handler.json
}

# ─── Auto-accept handler (opt-in) ───────────────────────────────────────────
resource "aws_iam_role" "auto_accept" {
  count = local.enable_auto_accept ? 1 : 0

  name               = local.names.auto_accept
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "auto_accept" {
  count = local.enable_auto_accept ? 1 : 0

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.auto_accept[0].arn}:*"]
  }

  statement {
    sid       = "SellerAllowListRead"
    actions   = local.dynamodb_read_actions
    resources = [aws_dynamodb_table.allowed_sellers.arn]
  }

  statement {
    sid       = "PublishNotifications"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.notifications.arn]
  }

  statement {
    sid = "MarketplaceDiscoveryRead"
    actions = [
      "aws-marketplace:GetOffer",
      "aws-marketplace:GetOfferTerms",
      "aws-marketplace:ListPurchaseOptions",
    ]
    resources = ["*"]
  }

  statement {
    sid = "MarketplaceAgreementAccept"
    actions = [
      "aws-marketplace:AcceptAgreementRequest",
      "aws-marketplace:CreateAgreementRequest",
      # Used to reconcile the accepted agreement's proposer account against
      # the seller allow-list.
      "aws-marketplace:DescribeAgreement",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "auto_accept" {
  count = local.enable_auto_accept ? 1 : 0

  name   = local.names.auto_accept
  role   = aws_iam_role.auto_accept[0].id
  policy = data.aws_iam_policy_document.auto_accept[0].json
}
