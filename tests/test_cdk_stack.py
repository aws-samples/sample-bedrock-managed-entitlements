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

import pytest

# Add cdk dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cdk"))

from aws_cdk.assertions import Match


def get_template():
    """Synthesize the CDK stack and return the CloudFormation template."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template
    from mppo_stack import MppoGrantsAutomationStack

    app = cdk.App()
    stack = MppoGrantsAutomationStack(app, "TestStack")
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
