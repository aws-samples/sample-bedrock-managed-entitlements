"""Tests for existing-license backfill planning."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_grants import build_backfill_plan, license_matches_seller


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
