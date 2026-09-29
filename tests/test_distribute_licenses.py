"""Tests for the lightweight license distribution script."""

import os
import sys
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lightweight"))

from distribute_licenses import (
    LICENSE_MANAGER_REGION,
    activate_grant,
    create_grant,
    main,
    plan_licenses,
    validate_apply_context,
)


def _grant(status):
    return {
        "GrantArn": "arn:aws:license-manager::111122223333:grant:g-test",
        "GrantStatus": status,
        "Version": "1",
    }


def _license(license_id, product_name, status="AVAILABLE", license_name=None):
    return {
        "LicenseArn": "arn:aws:license-manager::111122223333:license:%s" % license_id,
        "LicenseName": license_name,
        "ProductName": product_name,
        "Status": status,
    }


def _org_activity_error():
    return ClientError(
        {
            "Error": {
                "Code": "ResourceLimitExceededException",
                "Message": "Too many concurrent org grants are in progress.",
            }
        },
        "CreateGrant",
    )


def test_activate_grant_submits_activation_after_workflow_completed():
    lm = MagicMock()
    lm.create_grant_version.return_value = {"Version": "2"}

    result = activate_grant(lm, _grant("WORKFLOW_COMPLETED"))

    assert result is True
    lm.create_grant_version.assert_called_once()
    _, kwargs = lm.create_grant_version.call_args
    assert kwargs["GrantArn"] == "arn:aws:license-manager::111122223333:grant:g-test"
    assert kwargs["Status"] == "ACTIVE"
    assert kwargs["SourceVersion"] == "1"
    assert kwargs["Options"] == {
        "ActivationOverrideBehavior": "ALL_GRANTS_PERMITTED_BY_ISSUER"
    }


def test_activate_grant_skips_only_when_already_active():
    lm = MagicMock()

    result = activate_grant(lm, _grant("ACTIVE"))

    assert result is False
    lm.create_grant_version.assert_not_called()


def test_validate_apply_context_requires_matching_account_id():
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}

    assert validate_apply_context(sts, "123456789012") == 0
    assert validate_apply_context(sts, "210987654321") == 1


def test_plan_licenses_scopes_to_bedrock_names():
    licenses = [
        _license("l-product", "Amazon Bedrock managed entitlement"),
        _license("l-name", "Marketplace offer", license_name="Bedrock private offer"),
        _license("l-other", "Unrelated SaaS product"),
        _license("l-expired", "Amazon Bedrock expired entitlement", status="EXPIRED"),
    ]

    planned, ignored, excluded = plan_licenses(licenses)

    assert [lic["LicenseArn"].rsplit(":", 1)[-1] for lic in planned] == [
        "l-product",
        "l-name",
    ]
    assert [lic["LicenseArn"].rsplit(":", 1)[-1] for lic in ignored] == ["l-expired"]
    assert [lic["LicenseArn"].rsplit(":", 1)[-1] for lic in excluded] == ["l-other"]


def test_main_pins_license_manager_to_us_east_1():
    lm = MagicMock()
    lm.list_received_licenses.return_value = {"Licenses": []}
    orgs = MagicMock()
    orgs.describe_organization.return_value = {
        "Organization": {
            "Id": "o-test",
            "Arn": "arn:aws:organizations::111122223333:organization/o-test",
        }
    }

    def client(service_name, **kwargs):
        if service_name == "license-manager":
            assert kwargs == {"region_name": LICENSE_MANAGER_REGION}
            return lm
        if service_name == "organizations":
            assert kwargs == {}
            return orgs
        raise AssertionError("unexpected client: %s" % service_name)

    with patch.object(sys, "argv", ["distribute_licenses.py"]):
        with patch("distribute_licenses.boto3.client", side_effect=client):
            assert main() == 0


def test_create_grant_retries_when_org_activity_cap_is_hit():
    lm = MagicMock()
    lm.meta.region_name = "us-east-1"
    lm.create_grant.side_effect = [
        _org_activity_error(),
        {
            "GrantArn": "arn:aws:license-manager::111122223333:grant:g-child",
            "Version": "2",
        },
    ]
    lic = {
        "LicenseArn": "arn:aws:license-manager::111122223333:license:l-test",
        "HomeRegion": "us-east-1",
    }

    with patch("distribute_licenses.time.sleep") as sleep:
        result = create_grant(
            lm,
            lic,
            "arn:aws:organizations::111122223333:organization/o-test",
            ["CheckoutLicense"],
            "Grant to my organization",
        )

    assert result == "arn:aws:license-manager::111122223333:grant:g-child"
    assert lm.create_grant.call_count == 2
    sleep.assert_called_once_with(30)


def test_activate_grant_retries_when_org_activity_cap_is_hit():
    lm = MagicMock()
    lm.create_grant_version.side_effect = [
        _org_activity_error(),
        {"Version": "2"},
    ]

    with patch("distribute_licenses.time.sleep") as sleep:
        result = activate_grant(lm, _grant("WORKFLOW_COMPLETED"))

    assert result is True
    assert lm.create_grant_version.call_count == 2
    sleep.assert_called_once_with(30)
