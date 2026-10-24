"""Tests for seller allow-list seeding."""

from scripts.seed_sellers import build_seller_item


def test_build_seller_item_preserves_reconciliation_scope():
    seller = {
        "proposerAccountId": "111122223333",
        "name": "Example seller",
        "issuerName": "AWS/Marketplace",
        "productSkus": ["prod-one"],
        "productNames": ["Example model"],
        "grantTargets": ["o-exampleorgid"],
        "sellerProfileId": "seller-example",
        "autoAcceptOffers": True,
    }

    item = build_seller_item(seller)

    assert item["productSkus"] == ["prod-one"]
    assert item["productNames"] == ["Example model"]
    assert item["grantTargets"] == ["o-exampleorgid"]
    assert item["sellerProfileId"] == "seller-example"


def test_build_seller_item_omits_empty_optional_fields():
    item = build_seller_item({
        "proposerAccountId": "111122223333",
        "issuerName": "AWS/Marketplace",
        "productSkus": [],
        "productNames": [],
    })

    assert "productSkus" not in item
    assert "productNames" not in item
