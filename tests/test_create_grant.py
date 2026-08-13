"""Tests for License Manager grant activation."""

import os
import sys
from unittest.mock import patch

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from steps.create_grant import (
    DEFAULT_ALLOWED_OPERATIONS,
    _activate_grant,
    _create_single_grant,
    _resolve_allowed_operations,
)


class FakeLicenseManager:
    """Minimal fake for grant status polling tests."""

    def __init__(self, statuses, activation_response):
        self.statuses = list(statuses)
        self.activation_response = activation_response
        self.create_grant_version_calls = []

    def get_grant(self, GrantArn):
        if len(self.statuses) > 1:
            status, version = self.statuses.pop(0)
        else:
            status, version = self.statuses[0]

        return {
            "Grant": {
                "GrantArn": GrantArn,
                "GrantStatus": status,
                "Version": version,
            }
        }

    def create_grant_version(self, **kwargs):
        self.create_grant_version_calls.append(kwargs)
        return self.activation_response


class FakeOperationsClient:
    """Fake License Manager client for allowed operation derivation."""

    def __init__(self, licenses=None, parent_grant=None):
        self.licenses = licenses or []
        self.parent_grant = parent_grant or {}

    def list_received_licenses(self, **kwargs):
        return {"Licenses": self.licenses}

    def get_grant(self, GrantArn):
        return {"Grant": self.parent_grant}


class FakeCreateGrantClient:
    """Fake License Manager client for create-grant idempotency tests."""

    def __init__(self):
        self.create_grant_calls = []
        self.list_distributed_grants_calls = []

    def create_grant(self, **kwargs):
        self.create_grant_calls.append(kwargs)
        raise ClientError(
            {
                "Error": {
                    "Code": "ConflictException",
                    "Message": "License already has a grant for this principal",
                }
            },
            "CreateGrant",
        )

    def list_distributed_grants(self, **kwargs):
        self.list_distributed_grants_calls.append(kwargs)
        return {
            "Grants": [
                {
                    "GrantArn": "arn:aws:license-manager::111122223333:grant:g-existing"
                }
            ]
        }


class FakeStsClient:
    def get_caller_identity(self):
        return {"Account": "111122223333"}


def test_activate_grant_waits_for_disabled_then_workflow_completed():
    """Activation waits through delayed License Manager workflow states."""
    client = FakeLicenseManager(
        statuses=[
            ("PENDING_WORKFLOW", "1"),
            ("PENDING_WORKFLOW", "1"),
            ("DISABLED", "2"),
            ("PENDING_WORKFLOW", "3"),
            ("WORKFLOW_COMPLETED", "4"),
        ],
        activation_response={"Status": "PENDING_WORKFLOW", "Version": "3"},
    )

    with patch("steps.create_grant.time.sleep", return_value=None):
        result = _activate_grant(
            client,
            "arn:aws:license-manager::111122223333:grant:g-test",
            max_retries=6,
            poll_interval_seconds=0,
        )

    assert result == "WORKFLOW_COMPLETED"
    assert len(client.create_grant_version_calls) == 1
    assert client.create_grant_version_calls[0]["Status"] == "ACTIVE"
    assert client.create_grant_version_calls[0]["SourceVersion"] == "2"
    assert client.create_grant_version_calls[0]["Options"] == {
        "ActivationOverrideBehavior": "DISTRIBUTED_GRANTS_ONLY"
    }


def test_activate_grant_uses_replace_legacy_override():
    """Legacy cleanup mode passes the stronger activation override."""
    client = FakeLicenseManager(
        statuses=[
            ("DISABLED", "7"),
            ("WORKFLOW_COMPLETED", "8"),
        ],
        activation_response={"Status": "PENDING_WORKFLOW", "Version": "8"},
    )

    with patch("steps.create_grant.time.sleep", return_value=None):
        result = _activate_grant(
            client,
            "arn:aws:license-manager::111122223333:grant:g-test",
            max_retries=3,
            poll_interval_seconds=0,
            replace_legacy=True,
        )

    assert result == "WORKFLOW_COMPLETED"
    assert client.create_grant_version_calls[0]["Options"] == {
        "ActivationOverrideBehavior": "ALL_GRANTS_PERMITTED_BY_ISSUER"
    }


def test_activate_grant_times_out_without_disabled_state():
    """Activation remains pending if License Manager never reaches DISABLED."""
    client = FakeLicenseManager(
        statuses=[
            ("PENDING_WORKFLOW", "1"),
        ],
        activation_response={"Status": "PENDING_WORKFLOW", "Version": "2"},
    )

    with patch("steps.create_grant.time.sleep", return_value=None):
        result = _activate_grant(
            client,
            "arn:aws:license-manager::111122223333:grant:g-test",
            max_retries=3,
            poll_interval_seconds=0,
        )

    assert result == "ACTIVATION_PENDING"
    assert client.create_grant_version_calls == []


def test_resolve_allowed_operations_derives_from_parent_grant():
    """Grant operations follow the parent grant when License Manager exposes it."""
    client = FakeOperationsClient(
        licenses=[
            {
                "LicenseArn": "arn:aws:license-manager::111122223333:license:l-test",
                "LicenseMetadata": [
                    {
                        "Name": "grantArn",
                        "Value": "arn:aws:license-manager::111122223333:grant:g-parent",
                    }
                ],
            }
        ],
        parent_grant={
            "AllowedOperations": [
                "CreateGrant",
                "CheckoutLicense",
                "CheckoutLicense",
                "CreateToken",
            ]
        },
    )

    operations = _resolve_allowed_operations(
        client,
        "arn:aws:license-manager::111122223333:license:l-test",
    )

    assert operations == ["CheckoutLicense", "CreateToken"]


def test_resolve_allowed_operations_falls_back_to_defaults():
    """Missing parent grant metadata keeps the current Bedrock defaults."""
    client = FakeOperationsClient(licenses=[])

    operations = _resolve_allowed_operations(
        client,
        "arn:aws:license-manager::111122223333:license:l-test",
    )

    assert operations == DEFAULT_ALLOWED_OPERATIONS


def test_resolve_allowed_operations_honors_explicit_override():
    """Explicit operations are normalised and do not call License Manager."""
    client = FakeOperationsClient(licenses=[])

    operations = _resolve_allowed_operations(
        client,
        "arn:aws:license-manager::111122223333:license:l-test",
        allowed_operations=["CreateGrant", "CheckoutLicense", "CheckoutLicense"],
    )

    assert operations == ["CheckoutLicense"]


def test_create_single_grant_reuses_exact_existing_duplicate_grant():
    """Duplicate grant errors become idempotent only after exact grant lookup."""
    lm_client = FakeCreateGrantClient()

    def fake_boto3_client(service_name, region_name=None):
        if service_name == "license-manager":
            return lm_client
        if service_name == "sts":
            return FakeStsClient()
        raise AssertionError(f"Unexpected client: {service_name}")

    with patch("steps.create_grant.boto3.client", side_effect=fake_boto3_client):
        result = _create_single_grant(
            license_arn="arn:aws:license-manager::111122223333:license:l-test",
            target={"type": "organization", "id": "o-exampleorg"},
            seller_name="ISV Partner",
            product_name="Model",
            auto_activate=False,
            allowed_operations=["CheckoutLicense"],
        )

    assert result["grant_arn"] == "arn:aws:license-manager::111122223333:grant:g-existing"
    assert result["status"] == "EXISTING"
    assert lm_client.create_grant_calls[0]["AllowedOperations"] == ["CheckoutLicense"]
    assert lm_client.list_distributed_grants_calls[0]["Filters"] == [
        {
            "Name": "LicenseArn",
            "Values": ["arn:aws:license-manager::111122223333:license:l-test"],
        },
        {
            "Name": "GranteePrincipalARN",
            "Values": ["arn:aws:organizations::111122223333:organization/o-exampleorg"],
        },
    ]
