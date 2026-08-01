"""Tests for the seller verification step."""

import os
import sys

import boto3
import pytest
from moto import mock_aws

# Add lambda dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))


@pytest.fixture
def seller_table():
    """Create and seed a mocked DynamoDB seller table."""
    with mock_aws():
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
                "autoActivateGrant": True,
            }
        )
        table.put_item(
            Item={
                "proposerAccountId": "777788889999",
                "name": "AnotherVendor",
                "autoActivateGrant": False,
            }
        )
        yield table


@mock_aws
def test_verify_seller_found(seller_table):
    """Known seller returns config."""
    from steps.verify_seller import verify_seller

    result = verify_seller(
        proposer_account_id="444455556666",
        table_name="mppo-allowed-sellers",
    )
    assert result is not None
    assert result["name"] == "Anthropic"
    assert result["autoActivateGrant"] is True


@mock_aws
def test_verify_seller_not_found(seller_table):
    """Unknown seller returns None."""
    from steps.verify_seller import verify_seller

    result = verify_seller(
        proposer_account_id="999999999999",
        table_name="mppo-allowed-sellers",
    )
    assert result is None


@mock_aws
def test_verify_seller_empty_id():
    """Empty proposer ID returns None."""
    with mock_aws():
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

        from steps.verify_seller import verify_seller

        result = verify_seller(
            proposer_account_id="",
            table_name="mppo-allowed-sellers",
        )
        assert result is None


@mock_aws
def test_verify_seller_auto_activate_false(seller_table):
    """Seller with autoActivateGrant=False is still returned (decision is in handler)."""
    from steps.verify_seller import verify_seller

    result = verify_seller(
        proposer_account_id="777788889999",
        table_name="mppo-allowed-sellers",
    )
    assert result is not None
    assert result["name"] == "AnotherVendor"
    assert result["autoActivateGrant"] is False
