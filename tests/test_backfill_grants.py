"""Tests for existing-license backfill planning."""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_grants import (
    BackfillPlan,
    BackfillPlanItem,
    apply_plan,
    build_backfill_plan,
    license_matches_seller,
)


def _config(seller):
    return {
        "organizationId": "o-exampleorg",
        "allowedSellers": [seller],
    }


def _license(**overrides):
    license_record = {
        "LicenseArn": "arn:aws:license-manager::111122223333:license:l-test",
        "Issuer": {"Name": "AWS/Marketplace"},
        "ProductName": "ISV Model",
        "ProductSKU": "prod-example",
        "Status": "AVAILABLE",
    }
    license_record.update(overrides)
    return license_record


def test_backfill_blocks_issuer_only_matching():
    """Issuer-only matching is too broad because proposer account is absent."""
    plan = build_backfill_plan(
        config=_config({
            "name": "ISV Partner",
            "proposerAccountId": "123456789012",
            "issuerName": "AWS/Marketplace",
        }),
        licenses=[_license()],
    )

    assert plan.items == []
    assert plan.blocked == [
        "ISV Partner: issuer-only match is too broad; add productSkus, "
        "productNames, or pass --license-arn"
    ]


def test_backfill_matches_product_sku_filter():
    plan = build_backfill_plan(
        config=_config({
            "name": "ISV Partner",
            "proposerAccountId": "123456789012",
            "issuerName": "AWS/Marketplace",
            "productSkus": ["prod-example"],
        }),
        licenses=[_license()],
    )

    assert len(plan.items) == 1
    assert plan.items[0].grant_targets == [
        {"type": "organization", "id": "o-exampleorg"}
    ]
    assert plan.blocked == []


def test_backfill_matches_explicit_license_arn_without_product_filter():
    license_arn = "arn:aws:license-manager::111122223333:license:l-test"
    plan = build_backfill_plan(
        config=_config({
            "name": "ISV Partner",
            "proposerAccountId": "123456789012",
            "issuerName": "AWS/Marketplace",
        }),
        licenses=[_license()],
        license_arns=[license_arn],
    )

    assert len(plan.items) == 1
    assert plan.items[0].license_record["LicenseArn"] == license_arn


def test_backfill_uses_configured_grant_targets():
    targets = [{"type": "account", "id": "222233334444"}]
    plan = build_backfill_plan(
        config=_config({
            "name": "ISV Partner",
            "proposerAccountId": "123456789012",
            "issuerName": "AWS/Marketplace",
            "productNames": ["ISV Model"],
            "grantTargets": targets,
        }),
        licenses=[_license()],
    )

    assert len(plan.items) == 1
    assert plan.items[0].grant_targets == targets


def test_license_match_ignores_expired_license():
    assert not license_matches_seller(
        _license(Status="EXPIRED"),
        {
            "issuerName": "AWS/Marketplace",
            "productSkus": ["prod-example"],
        },
        explicit_license_arns=set(),
    )


def test_backfill_can_limit_to_seller_account():
    plan = build_backfill_plan(
        config={
            "organizationId": "o-exampleorg",
            "allowedSellers": [
                {
                    "name": "A",
                    "proposerAccountId": "111111111111",
                    "issuerName": "AWS/Marketplace",
                    "productSkus": ["prod-example"],
                },
                {
                    "name": "B",
                    "proposerAccountId": "222222222222",
                    "issuerName": "AWS/Marketplace",
                    "productSkus": ["prod-example"],
                },
            ],
        },
        licenses=[_license()],
        seller_accounts=["222222222222"],
    )

    assert len(plan.items) == 1
    assert plan.items[0].seller["name"] == "B"


def _plan_item(auto_activate=True):
    return BackfillPlanItem(
        seller={
            "name": "ISV Partner",
            "proposerAccountId": "123456789012",
            "autoActivateGrant": auto_activate,
            "replaceLegacyGrants": False,
        },
        license_record=_license(),
        grant_targets=[{"type": "organization", "id": "o-exampleorg"}],
    )


@patch("backfill_grants.record_pending_grant")
@patch("backfill_grants.create_and_activate_grants")
def test_apply_plan_records_pending_grant_when_not_immediately_active(
    mock_create, mock_record,
):
    """A grant that lands in DISABLED/PENDING_WORKFLOW after creation must be
    recorded in the pending-grants table, the same way handler.py's
    EventBridge-triggered path does -- otherwise the scheduled
    mppo-grant-activation-retry rule never learns about it and it never
    activates automatically.
    """
    mock_create.return_value = [{
        "grant_arn": "arn:aws:license-manager::111122223333:grant:g-test",
        "status": "DISABLED",
        "target_type": "organization",
        "target_id": "o-exampleorg",
    }]

    plan = BackfillPlan(items=[_plan_item()], skipped=[], blocked=[])
    exit_code = apply_plan(plan, region="us-east-1", organization_id="o-exampleorg")

    assert exit_code == 0
    mock_record.assert_called_once()
    _, kwargs = mock_record.call_args
    assert kwargs["grant"]["grant_arn"] == "arn:aws:license-manager::111122223333:grant:g-test"
    assert kwargs["license_arn"] == _license()["LicenseArn"]
    assert kwargs["seller_name"] == "ISV Partner"


@patch("backfill_grants.record_pending_grant")
@patch("backfill_grants.create_and_activate_grants")
def test_apply_plan_does_not_record_pending_grant_when_already_active(
    mock_create, mock_record,
):
    """A grant that activates immediately (ACTIVE or WORKFLOW_COMPLETED) needs
    no retry tracking."""
    mock_create.return_value = [{
        "grant_arn": "arn:aws:license-manager::111122223333:grant:g-test",
        "status": "WORKFLOW_COMPLETED",
        "target_type": "organization",
        "target_id": "o-exampleorg",
    }]

    plan = BackfillPlan(items=[_plan_item()], skipped=[], blocked=[])
    exit_code = apply_plan(plan, region="us-east-1", organization_id="o-exampleorg")

    assert exit_code == 0
    mock_record.assert_not_called()


@patch("backfill_grants.record_pending_grant")
@patch("backfill_grants.create_and_activate_grants")
def test_apply_plan_does_not_record_pending_grant_when_auto_activate_disabled(
    mock_create, mock_record,
):
    """A grant intentionally left DISABLED because autoActivateGrant is False
    is not a retry candidate -- the operator chose not to activate it."""
    mock_create.return_value = [{
        "grant_arn": "arn:aws:license-manager::111122223333:grant:g-test",
        "status": "DISABLED",
        "target_type": "organization",
        "target_id": "o-exampleorg",
    }]

    plan = BackfillPlan(items=[_plan_item(auto_activate=False)], skipped=[], blocked=[])
    exit_code = apply_plan(plan, region="us-east-1", organization_id="o-exampleorg")

    assert exit_code == 0
    mock_record.assert_not_called()


@patch("backfill_grants.record_pending_grant")
@patch("backfill_grants.create_and_activate_grants")
def test_apply_plan_uses_custom_pending_grant_table_name(mock_create, mock_record):
    mock_create.return_value = [{
        "grant_arn": "arn:aws:license-manager::111122223333:grant:g-test",
        "status": "PENDING_WORKFLOW",
        "target_type": "organization",
        "target_id": "o-exampleorg",
    }]

    plan = BackfillPlan(items=[_plan_item()], skipped=[], blocked=[])
    apply_plan(
        plan, region="us-east-1", organization_id="o-exampleorg",
        pending_grant_table_name="custom-pending-table",
    )

    _, kwargs = mock_record.call_args
    assert kwargs["table_name"] == "custom-pending-table"
