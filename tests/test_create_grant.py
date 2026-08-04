"""Tests for License Manager grant activation."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from steps.create_grant import _activate_grant


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
