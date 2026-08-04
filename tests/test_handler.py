"""Tests for the main Lambda handler."""

import json
import os
import sys

import boto3
import pytest
from moto import mock_aws
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))


def _load_fixture(name: str) -> dict:
    """Load a test fixture JSON file."""
    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures", name
    )
    with open(fixture_path) as f:
        return json.load(f)


@mock_aws
def test_handler_known_seller_full_flow():
    """Full flow: known seller → license found → grant created → notification sent."""
    # Setup mocked AWS resources
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="mppo-allowed-sellers",
        KeySchema=[
            {"AttributeName": "proposerAccountId", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "proposerAccountId", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(
        Item={
            "proposerAccountId": "444455556666",
            "name": "Anthropic",
            "issuerName": "Anthropic",
            "autoActivateGrant": True,
        }
    )

    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="mppo-grants-notifications")
    topic_arn = topic["TopicArn"]

    event = _load_fixture("sample-event.json")

    # Mock the license discovery and grant creation (these require real License Manager)
    mock_license_result = {
        "license_arn": "arn:aws:license-manager::111122223333:license:lic-test123",
        "product_name": "Claude Models",
        "product_sku": "prod-anthropic-claude",
        "issuer_name": "Anthropic",
    }
    mock_grant_result = {
        "grant_arn": "arn:aws:license-manager::111122223333:grant:g-test123",
        "status": "ACTIVE",
        "organization_id": "o-testorg",
        "license_arn": "arn:aws:license-manager::111122223333:license:lic-test123",
    }

    with patch.dict(os.environ, {
        "SELLER_TABLE_NAME": "mppo-allowed-sellers",
        "SNS_TOPIC_ARN": topic_arn,
        "ORGANIZATION_ID": "o-testorg",
        "HOME_REGION": "us-east-1",
    }):
        with patch("handler.discover_license", return_value=mock_license_result):
            with patch("handler.create_and_activate_grant", return_value=mock_grant_result):
                from handler import lambda_handler
                result = lambda_handler(event, None)

    assert result["status"] == "success"
    assert result["agreementId"] == "agmt-9xyz8wmklp67rt32nb1qv45ds"
    assert result["grantArn"] == mock_grant_result["grant_arn"]
    assert result["grantStatus"] == "ACTIVE"


@mock_aws
def test_handler_unknown_seller_skipped():
    """Unknown seller is skipped with notification."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="mppo-allowed-sellers",
        KeySchema=[
            {"AttributeName": "proposerAccountId", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "proposerAccountId", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="mppo-grants-notifications")
    topic_arn = topic["TopicArn"]

    event = _load_fixture("sample-event-unknown-seller.json")

    with patch.dict(os.environ, {
        "SELLER_TABLE_NAME": "mppo-allowed-sellers",
        "SNS_TOPIC_ARN": topic_arn,
        "ORGANIZATION_ID": "o-testorg",
        "HOME_REGION": "us-east-1",
    }):
        from handler import lambda_handler
        result = lambda_handler(event, None)

    assert result["status"] == "skipped"
    assert result["reason"] == "seller_not_allowed"


def test_handler_no_agreement_id():
    """Event with no agreement ID is skipped."""
    event = {
        "detail-type": "Purchase Agreement Created - Acceptor",
        "source": "aws.agreement-marketplace",
        "detail": {
            "agreement": {},
            "proposer": {"accountId": "444455556666"},
            "offer": {"id": "offer-123"},
        },
    }

    with patch.dict(os.environ, {
        "SELLER_TABLE_NAME": "mppo-allowed-sellers",
        "SNS_TOPIC_ARN": "",
        "ORGANIZATION_ID": "o-testorg",
        "HOME_REGION": "us-east-1",
    }):
        from handler import lambda_handler
        result = lambda_handler(event, None)

    assert result["status"] == "skipped"
    assert result["reason"] == "no_agreement_id"


def test_handler_no_proposer():
    """Event with no proposer account is skipped."""
    event = {
        "detail-type": "Purchase Agreement Created - Acceptor",
        "source": "aws.agreement-marketplace",
        "detail": {
            "agreement": {"id": "agmt-test123"},
            "proposer": {},
            "offer": {"id": "offer-123"},
        },
    }

    with patch.dict(os.environ, {
        "SELLER_TABLE_NAME": "mppo-allowed-sellers",
        "SNS_TOPIC_ARN": "",
        "ORGANIZATION_ID": "o-testorg",
        "HOME_REGION": "us-east-1",
    }):
        from handler import lambda_handler
        result = lambda_handler(event, None)

    assert result["status"] == "skipped"
    assert result["reason"] == "no_proposer_account"
