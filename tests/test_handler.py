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


# The exact target Inputs the scheduled EventBridge rules send (cdk/mppo_stack.py
# and terraform/eventbridge.tf). autospec=True makes a keyword-argument mismatch
# between the dispatch call and the step function raise, as it would in Lambda.
RETRY_RULE_INPUT = {
    "source": "mppo-grants-automation",
    "detail-type": "Retry Pending MPPO Grant Activations",
    "detail": {},
}
MONITOR_RULE_INPUT = {
    "source": "mppo-grants-automation",
    "detail-type": "Monitor MPPO Entitlements",
    "detail": {},
}


def test_retry_rule_input_dispatches_to_retry_with_matching_signature():
    """The retry rule's Input reaches retry_pending_grant_activations intact."""
    import handler

    with patch("handler.retry_pending_grant_activations", autospec=True) as retry, \
            patch("handler.monitor_entitlements", autospec=True) as monitor, \
            patch("handler.notify_admins", autospec=True):
        result = handler.lambda_handler(dict(RETRY_RULE_INPUT), None)

    retry.assert_called_once_with(
        table_name=handler.PENDING_GRANT_TABLE_NAME,
        home_region=handler.HOME_REGION,
        topic_arn=handler.SNS_TOPIC_ARN,
    )
    monitor.assert_not_called()
    assert result is retry.return_value


def test_monitor_rule_input_dispatches_to_monitor_with_matching_signature():
    """The monitor rule's Input reaches monitor_entitlements with the heartbeat name."""
    import handler

    with patch("handler.monitor_entitlements", autospec=True) as monitor, \
            patch("handler.retry_pending_grant_activations", autospec=True) as retry, \
            patch("handler.notify_admins", autospec=True):
        result = handler.lambda_handler(dict(MONITOR_RULE_INPUT), None)

    monitor.assert_called_once_with(
        seller_table_name=handler.SELLER_TABLE_NAME,
        pending_table_name=handler.PENDING_GRANT_TABLE_NAME,
        organization_id=handler.ORGANIZATION_ID,
        home_region=handler.HOME_REGION,
        topic_arn=handler.SNS_TOPIC_ARN,
        deployment_name=handler.DEPLOYMENT_NAME,
    )
    retry.assert_not_called()
    assert result is monitor.return_value


def test_rule_inputs_match_handler_dispatch_constants():
    """The scheduled payloads use the source and detail-types the handler checks."""
    import handler

    assert RETRY_RULE_INPUT["source"] == handler.RETRY_EVENT_SOURCE
    assert RETRY_RULE_INPUT["detail-type"] == handler.RETRY_EVENT_DETAIL_TYPE
    assert MONITOR_RULE_INPUT["source"] == handler.RETRY_EVENT_SOURCE
    assert MONITOR_RULE_INPUT["detail-type"] == handler.MONITOR_EVENT_DETAIL_TYPE
