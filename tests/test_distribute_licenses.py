"""Tests for the lightweight license distribution script."""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lightweight"))

from distribute_licenses import activate_grant, validate_apply_context


def _grant(status):
    return {
        "GrantArn": "arn:aws:license-manager::111122223333:grant:g-test",
        "GrantStatus": status,
        "Version": "1",
    }


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
