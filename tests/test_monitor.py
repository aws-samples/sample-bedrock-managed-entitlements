"""Tests for scheduled entitlement reconciliation."""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from steps.monitor import _emit_heartbeat, _find_issues


class FakeLicenseManager:
    def __init__(self, grants):
        self.grants = grants

    def list_distributed_grants(self, **kwargs):
        license_arn = kwargs["Filters"][0]["Values"][0]
        principal = kwargs["Filters"][1]["Values"][0]
        return {
            "Grants": [
                grant
                for grant in self.grants
                if grant["LicenseArn"] == license_arn
                and grant["GranteePrincipalArn"] == principal
            ]
        }


def _seller():
    return {
        "name": "Model Vendor",
        "proposerAccountId": "111122223333",
        "sellerProfileId": "sp-test",
        "issuerName": "Model Vendor",
        "productSkus": ["sku-test"],
    }


def _license():
    return {
        "LicenseArn": "arn:aws:license-manager::111122223333:license:l-test",
        "Issuer": {"Name": "Model Vendor"},
        "ProductSKU": "sku-test",
        "ProductName": "Model",
        "Status": "AVAILABLE",
    }


def test_monitor_reports_unaccepted_offer_and_missing_grant():
    issues = _find_issues(
        sellers=[_seller()],
        offers=[{
            "offer_id": "offer-test",
            "seller_profile_id": "sp-test",
            "seller_name": "Model Vendor",
        }],
        licenses=[_license()],
        pending=[],
        license_manager=FakeLicenseManager([]),
        organization_id="o-example1234",
        account_id="999900001111",
    )

    assert any("offer-test" in issue and "not been accepted" in issue for issue in issues)
    assert any("has no grant" in issue for issue in issues)


def test_monitor_is_healthy_with_active_grant():
    license_record = _license()
    principal = (
        "arn:aws:organizations::999900001111:"
        "organization/o-example1234"
    )
    issues = _find_issues(
        sellers=[_seller()],
        offers=[],
        licenses=[license_record],
        pending=[],
        license_manager=FakeLicenseManager([{
            "LicenseArn": license_record["LicenseArn"],
            "GranteePrincipalArn": principal,
            "GrantArn": "arn:aws:license-manager::999900001111:grant:g-test",
            "GrantStatus": "ACTIVE",
        }]),
        organization_id="o-example1234",
        account_id="999900001111",
    )

    assert issues == []


def test_monitor_reports_pending_activation_and_unsafe_config():
    seller = _seller()
    seller.pop("sellerProfileId")
    seller.pop("productSkus")

    issues = _find_issues(
        sellers=[seller],
        offers=[],
        licenses=[],
        pending=[{
            "grantArn": "grant-test",
            "sellerName": "Model Vendor",
            "productName": "Model",
            "lastStatus": "ACTIVATION_PENDING",
            "retryCount": 3,
        }],
        license_manager=FakeLicenseManager([]),
        organization_id="o-example1234",
        account_id="999900001111",
    )

    assert any("grant-test" in issue and "not active" in issue for issue in issues)
    assert any("sellerProfileId is missing" in issue for issue in issues)
    assert any("cannot be safely reconciled" in issue for issue in issues)


def test_monitor_heartbeat_uses_deployment_dimension():
    cloudwatch = MagicMock()
    with patch("steps.monitor.boto3.client", return_value=cloudwatch):
        _emit_heartbeat("us-east-1", "mppo-test")

    cloudwatch.put_metric_data.assert_called_once_with(
        Namespace="MPPO/Entitlements",
        MetricData=[{
            "MetricName": "ReconciliationCompleted",
            "Dimensions": [
                {"Name": "Deployment", "Value": "mppo-test"},
            ],
            "Value": 1,
            "Unit": "Count",
        }],
    )
