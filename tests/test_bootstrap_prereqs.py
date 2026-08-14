"""Tests for prerequisite bootstrap checks."""

from botocore.exceptions import ClientError

from scripts.bootstrap_prereqs import (
    LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL,
    MARKETPLACE_LICENSE_MANAGEMENT_PRINCIPAL,
    CheckResult,
    apply_delegated_admin,
    apply_license_manager_settings,
    apply_license_manager_trusted_access,
    apply_marketplace_trusted_access,
    check_delegated_admin,
    check_license_manager_settings,
    check_license_manager_trusted_access,
    check_marketplace_trusted_access,
    check_organization,
    check_service_linked_roles,
    validate_apply_confirmation,
)


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        self.kwargs = kwargs
        yield from self.pages


class FakeOrganizations:
    def __init__(
        self,
        org=None,
        service_principals=None,
        delegated_admins=None,
    ):
        self.org = org or {
            "Id": "o-exampleorgid",
            "FeatureSet": "ALL",
            "MasterAccountId": "111122223333",
        }
        self.service_principals = service_principals or []
        self.delegated_admins = delegated_admins or []
        self.enabled_principal = None
        self.registered_admin = None

    def describe_organization(self):
        return {"Organization": self.org}

    def get_paginator(self, operation_name):
        if operation_name == "list_aws_service_access_for_organization":
            return FakePaginator([{
                "EnabledServicePrincipals": [
                    {"ServicePrincipal": principal}
                    for principal in self.service_principals
                ]
            }])
        if operation_name == "list_delegated_administrators":
            return FakePaginator([{
                "DelegatedAdministrators": [
                    {"Id": account_id}
                    for account_id in self.delegated_admins
                ]
            }])
        raise AssertionError(f"Unexpected paginator: {operation_name}")

    def enable_aws_service_access(self, ServicePrincipal):
        self.enabled_principal = ServicePrincipal
        self.service_principals.append(ServicePrincipal)

    def register_delegated_administrator(self, AccountId, ServicePrincipal):
        self.registered_admin = (AccountId, ServicePrincipal)
        self.delegated_admins.append(AccountId)


class FakeLicenseManager:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.updated = False

    def get_service_settings(self):
        return {
            "OrganizationConfiguration": {
                "EnableIntegration": self.enabled,
            },
            "ServiceStatus": "ENABLED",
        }

    def update_service_settings(self, OrganizationConfiguration):
        self.updated = OrganizationConfiguration["EnableIntegration"]
        self.enabled = self.updated


class FakeIam:
    def __init__(self, existing_roles):
        self.existing_roles = set(existing_roles)

    def get_role(self, RoleName):
        if RoleName in self.existing_roles:
            return {"Role": {"RoleName": RoleName}}
        raise ClientError(
            {"Error": {"Code": "NoSuchEntity", "Message": "Role not found"}},
            "GetRole",
        )


def test_organization_requires_all_features():
    orgs = FakeOrganizations(org={
        "Id": "o-exampleorgid",
        "FeatureSet": "CONSOLIDATED_BILLING",
        "MasterAccountId": "111122223333",
    })

    result = check_organization(orgs, "111122223333")

    assert result.status == "MANUAL"
    assert result.blocker is True
    assert "Enable all features" in result.detail


def test_organization_requires_management_account():
    orgs = FakeOrganizations()

    result = check_organization(orgs, "999988887777")

    assert result.status == "FAIL"
    assert result.blocker is True
    assert "management account" in result.detail


def test_license_manager_settings_can_be_applied():
    lm = FakeLicenseManager(enabled=False)

    before = check_license_manager_settings(lm)
    after = apply_license_manager_settings(lm)

    assert before.status == "APPLY"
    assert before.blocker is True
    assert lm.updated is True
    assert after.status == "OK"


def test_marketplace_trusted_access_can_be_applied():
    orgs = FakeOrganizations(service_principals=[])

    before = check_marketplace_trusted_access(orgs)
    after = apply_marketplace_trusted_access(orgs)

    assert before.status == "APPLY"
    assert orgs.enabled_principal == MARKETPLACE_LICENSE_MANAGEMENT_PRINCIPAL
    assert after.status == "OK"


def test_license_manager_trusted_access_can_be_applied():
    """License Manager's own trusted access is a separate prerequisite from
    Marketplace trusted access -- both must be enabled before CreateGrant can
    distribute an org-wide grant. Missing this one produces a real API error
    ("Grantor has disabled Trusted Access to AWS License Manager Service in
    AWS Organizations") even when every other check passes.
    """
    orgs = FakeOrganizations(service_principals=[])

    before = check_license_manager_trusted_access(orgs)
    after = apply_license_manager_trusted_access(orgs)

    assert before.status == "APPLY"
    assert before.blocker is True
    assert orgs.enabled_principal == LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL
    assert after.status == "OK"


def test_license_manager_trusted_access_ok_when_already_enabled():
    orgs = FakeOrganizations(service_principals=[LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL])

    result = check_license_manager_trusted_access(orgs)

    assert result.status == "OK"
    assert result.blocker is False


def test_license_manager_trusted_access_is_independent_of_marketplace_trusted_access():
    """Enabling only the Marketplace principal must not satisfy the License
    Manager trusted-access check -- they are two distinct prerequisites."""
    orgs = FakeOrganizations(service_principals=[MARKETPLACE_LICENSE_MANAGEMENT_PRINCIPAL])

    marketplace_result = check_marketplace_trusted_access(orgs)
    lm_result = check_license_manager_trusted_access(orgs)

    assert marketplace_result.status == "OK"
    assert lm_result.status == "APPLY"
    assert lm_result.blocker is True


def test_service_linked_role_check_warns_for_missing_roles():
    iam = FakeIam(existing_roles=["AWSServiceRoleForAWSLicenseManagerRole"])

    result = check_service_linked_roles(iam)

    assert result.status == "WARN"
    assert result.blocker is False
    assert "Missing roles" in result.detail


def test_delegated_admin_is_opt_in():
    orgs = FakeOrganizations()

    result = check_delegated_admin(orgs, None)

    assert result == CheckResult(
        "License Manager delegated admin",
        "SKIP",
        "No delegated admin account requested.",
    )


def test_delegated_admin_can_be_applied():
    orgs = FakeOrganizations(delegated_admins=[])

    before = check_delegated_admin(orgs, "222233334444")
    after = apply_delegated_admin(orgs, "222233334444")

    assert before.status == "APPLY"
    assert orgs.registered_admin == (
        "222233334444",
        "license-manager.amazonaws.com",
    )
    assert after.status == "OK"


def test_apply_confirmation_skips_check_mode():
    result = validate_apply_confirmation(
        apply_changes=False,
        caller_account_id="111122223333",
        confirm_account_id=None,
        delegated_admin_account_id=None,
        confirm_delegated_admin_account_id=None,
    )

    assert result.status == "SKIP"
    assert result.blocker is False


def test_apply_confirmation_requires_current_account_match():
    result = validate_apply_confirmation(
        apply_changes=True,
        caller_account_id="111122223333",
        confirm_account_id=None,
        delegated_admin_account_id=None,
        confirm_delegated_admin_account_id=None,
    )

    assert result.status == "FAIL"
    assert result.blocker is True
    assert "--confirm-account-id" in result.detail
    assert "111122223333" in result.detail


def test_apply_confirmation_rejects_wrong_account():
    result = validate_apply_confirmation(
        apply_changes=True,
        caller_account_id="111122223333",
        confirm_account_id="999988887777",
        delegated_admin_account_id=None,
        confirm_delegated_admin_account_id=None,
    )

    assert result.status == "FAIL"
    assert result.blocker is True


def test_apply_confirmation_requires_delegated_admin_match():
    result = validate_apply_confirmation(
        apply_changes=True,
        caller_account_id="111122223333",
        confirm_account_id="111122223333",
        delegated_admin_account_id="222233334444",
        confirm_delegated_admin_account_id="333344445555",
    )

    assert result.name == "Delegated admin confirmation"
    assert result.status == "FAIL"
    assert result.blocker is True
    assert "--confirm-delegated-admin-account-id" in result.detail


def test_apply_confirmation_allows_confirmed_apply():
    result = validate_apply_confirmation(
        apply_changes=True,
        caller_account_id="111122223333",
        confirm_account_id="111122223333",
        delegated_admin_account_id="222233334444",
        confirm_delegated_admin_account_id="222233334444",
    )

    assert result.status == "OK"
    assert result.blocker is False
