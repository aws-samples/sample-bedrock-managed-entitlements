"""CDK stack assertion tests.

Validates that the deployed infrastructure matches expected configuration:
- EventBridge rule pattern is correct
- Lambda has correct permissions
- DynamoDB table schema is correct
- SNS topic is created
"""

import json
import os
import sys
from contextlib import contextmanager

import pytest

# Add cdk dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cdk"))

from aws_cdk.assertions import Match


@contextmanager
def isolated_config(config: dict | None = None):
    """Temporarily hide or replace local sellers.json during CDK synth tests."""
    config_dir = os.path.join(os.path.dirname(__file__), "..", "config")
    config_path = os.path.join(config_dir, "sellers.json")

    original = None
    if os.path.exists(config_path):
        with open(config_path) as f:
            original = f.read()

    try:
        if config is None:
            if os.path.exists(config_path):
                os.remove(config_path)
        else:
            with open(config_path, "w") as f:
                json.dump(config, f)
        yield
    finally:
        if original is None:
            if os.path.exists(config_path):
                os.remove(config_path)
        else:
            with open(config_path, "w") as f:
                f.write(original)


def get_template():
    """Synthesize the CDK stack and return the CloudFormation template."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template
    from mppo_stack import MppoGrantsAutomationStack

    with isolated_config():
        app = cdk.App()
        stack = MppoGrantsAutomationStack(app, "TestStack")
        return Template.from_stack(stack)


def get_template_with_auto_accept():
    """Synthesize the stack with enableAutoAccept=true in the config file."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template
    from mppo_stack import MppoGrantsAutomationStack

    config_dir = os.path.join(os.path.dirname(__file__), "..", "config")
    example_path = os.path.join(config_dir, "sellers.example.json")
    config_path = os.path.join(config_dir, "sellers.json")

    with open(example_path) as f:
        config = json.load(f)
    config["enableAutoAccept"] = True

    with isolated_config(config):
        app = cdk.App()
        stack = MppoGrantsAutomationStack(app, "TestStackAutoAccept")
        return Template.from_stack(stack)


def test_eventbridge_rule_pattern():
    """EventBridge rule matches correct source and detail-type."""
    template = get_template()

    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": {
                "source": ["aws.agreement-marketplace"],
                "detail-type": ["Purchase Agreement Created - Acceptor"],
            }
        },
    )


def test_dynamodb_table_schema():
    """DynamoDB table has correct key schema."""
    template = get_template()

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "proposerAccountId", "KeyType": "HASH"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_pending_grant_table_schema():
    """Pending grant table tracks activation retries by grant ARN."""
    template = get_template()

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "grantArn", "KeyType": "HASH"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_lambda_runtime():
    """Lambda uses Python 3.12 runtime."""
    template = get_template()

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.12",
            "Handler": "handler.lambda_handler",
            "Timeout": 300,
        },
    )


def test_lambda_environment_variables():
    """Lambda has required environment variables."""
    template = get_template()

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": {
                    "PENDING_GRANT_TABLE_NAME": {
                        "Ref": Match.any_value()
                    },
                    "HOME_REGION": "us-east-1",
                }
            }
        },
    )


def test_sns_topic_exists():
    """SNS notification topic is created."""
    template = get_template()

    template.has_resource_properties(
        "AWS::SNS::Topic",
        {
            "DisplayName": "MPPO Grants Automation Notifications",
        },
    )


def test_lambda_has_license_manager_permissions():
    """Lambda role includes License Manager permissions."""
    template = get_template()

    # Use Match.arrayWith to find the statement within the larger policy
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Action": [
                            "license-manager:ListReceivedLicenses",
                            "license-manager:CreateGrant",
                            "license-manager:CreateGrantVersion",
                            "license-manager:GetGrant",
                            "license-manager:ListDistributedGrants",
                        ],
                        "Effect": "Allow",
                    })
                ])
            }
        },
    )


def test_lambda_has_organizations_permissions_for_account_targets():
    """Lambda can validate account-scoped grant targets against the org."""
    template = get_template()

    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Action": [
                            "organizations:DescribeOrganization",
                            "organizations:ListAccounts",
                        ],
                        "Effect": "Allow",
                    })
                ])
            }
        },
    )


def test_activation_retry_rule_schedule():
    """Scheduled rule retries pending grant activations."""
    template = get_template()

    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "Name": "mppo-grant-activation-retry",
            "ScheduleExpression": "rate(6 hours)",
            "State": "ENABLED",
        },
    )


def test_auto_accept_not_deployed_by_default():
    """Auto-accept Lambda is opt-in and absent when enableAutoAccept is unset."""
    template = get_template()

    template.resource_properties_count_is(
        "AWS::Lambda::Function",
        {"FunctionName": "mppo-auto-accept-handler"},
        0,
    )


def test_auto_accept_lambda_deployed_when_enabled():
    """Auto-accept Lambda deploys with discovery + agreement IAM permissions."""
    template = get_template_with_auto_accept()

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "mppo-auto-accept-handler",
            "Handler": "auto_accept_handler.lambda_handler",
        },
    )

    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Sid": "MarketplaceDiscoveryRead",
                        "Effect": "Allow",
                        "Action": [
                            "aws-marketplace:ListPurchaseOptions",
                            "aws-marketplace:GetOffer",
                            "aws-marketplace:GetOfferTerms",
                        ],
                    })
                ])
            }
        },
    )

    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Sid": "MarketplaceAgreementAccept",
                        "Effect": "Allow",
                        "Action": [
                            "aws-marketplace:CreateAgreementRequest",
                            "aws-marketplace:AcceptAgreementRequest",
                        ],
                    })
                ])
            }
        },
    )

    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "Name": "mppo-auto-accept-schedule",
            "ScheduleExpression": "rate(1 hour)",
            "State": "ENABLED",
        },
    )
