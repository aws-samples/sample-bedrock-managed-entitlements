"""Tests for pending grant activation retry tracking."""

import os
import sys
from unittest.mock import patch

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from steps.pending_grants import (
    record_pending_grant,
    retry_pending_grant_activations,
)


def _create_pending_table():
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="mppo-pending-grants",
        KeySchema=[
            {"AttributeName": "grantArn", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "grantArn", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return table


@mock_aws
def test_record_pending_grant_writes_retry_item():
    """Pending grants are persisted for scheduled retry."""
    table = _create_pending_table()

    record_pending_grant(
        table_name="mppo-pending-grants",
        grant={"grant_arn": "arn:aws:license-manager::111122223333:grant:g-test", "status": "ACTIVATION_PENDING"},
        agreement_id="agmt-test",
        offer_id="offer-test",
        seller_name="ISV Partner",
        product_name="Partner ML Product",
        license_arn="arn:aws:license-manager::444455556666:license:l-test",
        replace_legacy_grants=False,
    )

    item = table.get_item(
        Key={"grantArn": "arn:aws:license-manager::111122223333:grant:g-test"}
    )["Item"]

    assert item["sellerName"] == "ISV Partner"
    assert item["lastStatus"] == "ACTIVATION_PENDING"
    assert item["retryCount"] == 0
    assert item["replaceLegacyGrants"] is False


@mock_aws
def test_retry_pending_grants_deletes_completed_item():
    """Completed activations are removed from the pending table."""
    table = _create_pending_table()
    table.put_item(
        Item={
            "grantArn": "arn:aws:license-manager::111122223333:grant:g-test",
            "licenseArn": "arn:aws:license-manager::444455556666:license:l-test",
            "agreementId": "agmt-test",
            "offerId": "offer-test",
            "sellerName": "ISV Partner",
            "productName": "Partner ML Product",
            "lastStatus": "ACTIVATION_PENDING",
            "replaceLegacyGrants": False,
            "retryCount": 0,
        }
    )

    with patch("steps.pending_grants._activate_grant", return_value="WORKFLOW_COMPLETED"):
        result = retry_pending_grant_activations(
            table_name="mppo-pending-grants",
            home_region="us-east-1",
            topic_arn="",
        )

    assert result == {
        "status": "success",
        "processed": 1,
        "completed": 1,
        "stillPending": 0,
        "failures": 0,
    }
    assert "Item" not in table.get_item(
        Key={"grantArn": "arn:aws:license-manager::111122223333:grant:g-test"}
    )


@mock_aws
def test_retry_pending_grants_updates_still_pending_item():
    """Still-pending activations keep retry state for the next schedule."""
    table = _create_pending_table()
    table.put_item(
        Item={
            "grantArn": "arn:aws:license-manager::111122223333:grant:g-test",
            "licenseArn": "arn:aws:license-manager::444455556666:license:l-test",
            "agreementId": "agmt-test",
            "offerId": "offer-test",
            "sellerName": "ISV Partner",
            "productName": "Partner ML Product",
            "lastStatus": "ACTIVATION_PENDING",
            "replaceLegacyGrants": True,
            "retryCount": 0,
        }
    )

    with patch("steps.pending_grants._activate_grant", return_value="ACTIVATION_PENDING"):
        result = retry_pending_grant_activations(
            table_name="mppo-pending-grants",
            home_region="us-east-1",
            topic_arn="",
        )

    item = table.get_item(
        Key={"grantArn": "arn:aws:license-manager::111122223333:grant:g-test"}
    )["Item"]

    assert result["stillPending"] == 1
    assert item["lastStatus"] == "ACTIVATION_PENDING"
    assert item["retryCount"] == 1
