"""Check and optionally bootstrap MPPO prerequisite services.

Run this from the AWS Organizations management account in us-east-1:

    python3 scripts/bootstrap_prereqs.py --check
    python3 scripts/bootstrap_prereqs.py --apply --confirm-account-id 123456789012
    python3 scripts/bootstrap_prereqs.py --apply --confirm-account-id 123456789012 \
        --delegated-admin-account-id 222233334444 \
        --confirm-delegated-admin-account-id 222233334444

The script keeps risky organization changes explicit:
- Organizations all-features is checked only.
- Apply mode requires confirmation of the current AWS account ID.
- Delegated administrator registration runs only when an account ID is passed.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Iterable

import boto3
from botocore.exceptions import ClientError


LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL = "license-manager.amazonaws.com"
MARKETPLACE_LICENSE_MANAGEMENT_PRINCIPAL = "license-management.marketplace.amazonaws.com"

LICENSE_MANAGER_ORG_ROLE = "AWSServiceRoleForAWSLicenseManagerMasterAccountRole"
LICENSE_MANAGER_ROLE = "AWSServiceRoleForAWSLicenseManagerRole"
MARKETPLACE_LICENSE_MANAGEMENT_ROLE = "AWSServiceRoleForMarketplaceLicenseManagement"


@dataclass(frozen=True)
class CheckResult:
    """A single prerequisite check result."""

    name: str
    status: str
    detail: str
    blocker: bool = False


def _client_error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "Unknown")


def _paginate(client, operation_name: str, result_key: str, **kwargs) -> Iterable[dict]:
    paginator = client.get_paginator(operation_name)
    for page in paginator.paginate(**kwargs):
        yield from page.get(result_key, [])


def check_organization(orgs_client, caller_account_id: str | None = None) -> CheckResult:
    """Validate that AWS Organizations exists and uses all-features mode."""
    try:
        org = orgs_client.describe_organization()["Organization"]
    except ClientError as error:
        code = _client_error_code(error)
        if code == "AWSOrganizationsNotInUseException":
            return CheckResult(
                "AWS Organizations",
                "FAIL",
                "Organizations is not enabled for this account.",
                blocker=True,
            )
        return CheckResult("AWS Organizations", "FAIL", str(error), blocker=True)

    org_id = org.get("Id", "unknown")
    feature_set = org.get("FeatureSet", "UNKNOWN")
    management_account = org.get("MasterAccountId") or org.get("ManagementAccountId")

    if caller_account_id and management_account and caller_account_id != management_account:
        return CheckResult(
            "Organizations management account",
            "FAIL",
            f"Current account {caller_account_id} is not the management account {management_account}.",
            blocker=True,
        )

    if feature_set != "ALL":
        return CheckResult(
            "Organizations all-features mode",
            "MANUAL",
            f"Organization {org_id} uses FeatureSet={feature_set}. Enable all features before org-wide grants.",
            blocker=True,
        )

    return CheckResult(
        "Organizations all-features mode",
        "OK",
        f"Organization {org_id} uses all features.",
    )


def check_license_manager_settings(lm_client) -> CheckResult:
    """Check whether License Manager is linked to AWS Organizations."""
    try:
        settings = lm_client.get_service_settings()
    except ClientError as error:
        return CheckResult("License Manager service settings", "FAIL", str(error), blocker=True)

    enabled = settings.get("OrganizationConfiguration", {}).get("EnableIntegration")
    service_status = settings.get("ServiceStatus", "UNKNOWN")
    if enabled:
        return CheckResult(
            "License Manager organization integration",
            "OK",
            f"Integration enabled; service status is {service_status}.",
        )

    return CheckResult(
        "License Manager organization integration",
        "APPLY",
        "Integration is disabled. Run with --apply --confirm-account-id <current-account-id> "
        "to enable Link AWS Organization accounts.",
        blocker=True,
    )


def apply_license_manager_settings(lm_client) -> CheckResult:
    """Enable License Manager organization integration."""
    try:
        lm_client.update_service_settings(
            OrganizationConfiguration={"EnableIntegration": True}
        )
    except ClientError as error:
        return CheckResult(
            "License Manager organization integration",
            "FAIL",
            str(error),
            blocker=True,
        )

    return check_license_manager_settings(lm_client)


def check_marketplace_trusted_access(orgs_client) -> CheckResult:
    """Check whether Marketplace license management has trusted access."""
    try:
        principals = _paginate(
            orgs_client,
            "list_aws_service_access_for_organization",
            "EnabledServicePrincipals",
        )
        enabled = any(
            principal.get("ServicePrincipal") == MARKETPLACE_LICENSE_MANAGEMENT_PRINCIPAL
            for principal in principals
        )
    except ClientError as error:
        if _client_error_code(error) == "AWSOrganizationsNotInUseException":
            return CheckResult(
                "Marketplace trusted access",
                "FAIL",
                "AWS Organizations is not enabled for this account.",
                blocker=True,
            )
        return CheckResult("Marketplace trusted access", "FAIL", str(error), blocker=True)

    if enabled:
        return CheckResult(
            "Marketplace trusted access",
            "OK",
            f"{MARKETPLACE_LICENSE_MANAGEMENT_PRINCIPAL} is enabled.",
        )

    return CheckResult(
        "Marketplace trusted access",
        "APPLY",
        f"{MARKETPLACE_LICENSE_MANAGEMENT_PRINCIPAL} is not enabled. Run with "
        "--apply --confirm-account-id <current-account-id> to enable it.",
        blocker=True,
    )


def apply_marketplace_trusted_access(orgs_client) -> CheckResult:
    """Enable AWS Marketplace license management trusted access."""
    try:
        orgs_client.enable_aws_service_access(
            ServicePrincipal=MARKETPLACE_LICENSE_MANAGEMENT_PRINCIPAL
        )
    except ClientError as error:
        return CheckResult("Marketplace trusted access", "FAIL", str(error), blocker=True)

    return check_marketplace_trusted_access(orgs_client)


def check_license_manager_trusted_access(orgs_client) -> CheckResult:
    """Check whether AWS License Manager itself has trusted access.

    This is a separate prerequisite from Marketplace trusted access
    (MARKETPLACE_LICENSE_MANAGEMENT_PRINCIPAL) and from delegated admin
    registration. License Manager's own service principal must have trusted
    access enabled in AWS Organizations before license-manager:CreateGrant can
    distribute a grant to an organization or OU target -- without it,
    CreateGrant fails with "Grantor has disabled Trusted Access to AWS License
    Manager Service in AWS Organizations", even when every other prerequisite
    (all-features mode, License Manager org integration, Marketplace trusted
    access, service-linked roles) is already satisfied. This check exists
    specifically to catch that gap before a real grant creation call hits it.
    """
    try:
        principals = _paginate(
            orgs_client,
            "list_aws_service_access_for_organization",
            "EnabledServicePrincipals",
        )
        enabled = any(
            principal.get("ServicePrincipal") == LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL
            for principal in principals
        )
    except ClientError as error:
        if _client_error_code(error) == "AWSOrganizationsNotInUseException":
            return CheckResult(
                "License Manager trusted access",
                "FAIL",
                "AWS Organizations is not enabled for this account.",
                blocker=True,
            )
        return CheckResult("License Manager trusted access", "FAIL", str(error), blocker=True)

    if enabled:
        return CheckResult(
            "License Manager trusted access",
            "OK",
            f"{LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL} is enabled.",
        )

    return CheckResult(
        "License Manager trusted access",
        "APPLY",
        f"{LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL} is not enabled. Run with "
        "--apply --confirm-account-id <current-account-id> to enable it. Without this, "
        "CreateGrant fails with 'Grantor has disabled Trusted Access to AWS License "
        "Manager Service in AWS Organizations' even if every other check passes.",
        blocker=True,
    )


def apply_license_manager_trusted_access(orgs_client) -> CheckResult:
    """Enable AWS License Manager's own trusted access."""
    try:
        orgs_client.enable_aws_service_access(
            ServicePrincipal=LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL
        )
    except ClientError as error:
        return CheckResult("License Manager trusted access", "FAIL", str(error), blocker=True)

    return check_license_manager_trusted_access(orgs_client)


def check_service_linked_roles(iam_client) -> CheckResult:
    """Check service-linked roles created by License Manager and Marketplace."""
    role_names = [
        LICENSE_MANAGER_ORG_ROLE,
        LICENSE_MANAGER_ROLE,
        MARKETPLACE_LICENSE_MANAGEMENT_ROLE,
    ]
    missing = []

    for role_name in role_names:
        try:
            iam_client.get_role(RoleName=role_name)
        except ClientError as error:
            if _client_error_code(error) == "NoSuchEntity":
                missing.append(role_name)
                continue
            return CheckResult("Service-linked roles", "FAIL", str(error), blocker=True)

    if missing:
        return CheckResult(
            "Service-linked roles",
            "WARN",
            "Missing roles: "
            + ", ".join(missing)
            + ". They are usually created by License Manager or Marketplace trusted access setup.",
        )

    return CheckResult("Service-linked roles", "OK", "Required management-account roles exist.")


def check_delegated_admin(orgs_client, account_id: str | None) -> CheckResult:
    """Check delegated admin only when the caller opts into that topology."""
    if not account_id:
        return CheckResult(
            "License Manager delegated admin",
            "SKIP",
            "No delegated admin account requested.",
        )

    try:
        admins = _paginate(
            orgs_client,
            "list_delegated_administrators",
            "DelegatedAdministrators",
            ServicePrincipal=LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL,
        )
        enabled = any(admin.get("Id") == account_id for admin in admins)
    except ClientError as error:
        return CheckResult("License Manager delegated admin", "FAIL", str(error), blocker=True)

    if enabled:
        return CheckResult(
            "License Manager delegated admin",
            "OK",
            f"Account {account_id} is registered.",
        )

    return CheckResult(
        "License Manager delegated admin",
        "APPLY",
        f"Account {account_id} is not registered. Run with --apply, "
        "--confirm-account-id, and --confirm-delegated-admin-account-id to register it.",
        blocker=True,
    )


def apply_delegated_admin(orgs_client, account_id: str | None) -> CheckResult:
    """Register a License Manager delegated admin account."""
    if not account_id:
        return check_delegated_admin(orgs_client, account_id)

    try:
        orgs_client.register_delegated_administrator(
            AccountId=account_id,
            ServicePrincipal=LICENSE_MANAGER_DELEGATED_ADMIN_PRINCIPAL,
        )
    except ClientError as error:
        code = _client_error_code(error)
        if code not in {"AccountAlreadyRegisteredException", "DuplicateAccountException"}:
            return CheckResult("License Manager delegated admin", "FAIL", str(error), blocker=True)

    return check_delegated_admin(orgs_client, account_id)


def get_caller_account_id(sts_client) -> str:
    """Return the current caller account ID."""
    return sts_client.get_caller_identity()["Account"]


def validate_apply_confirmation(
    apply_changes: bool,
    caller_account_id: str,
    confirm_account_id: str | None,
    delegated_admin_account_id: str | None,
    confirm_delegated_admin_account_id: str | None,
) -> CheckResult:
    """Require explicit account confirmations before mutating org settings."""
    if not apply_changes:
        return CheckResult("Apply confirmation", "SKIP", "Check mode does not change AWS resources.")

    if confirm_account_id != caller_account_id:
        return CheckResult(
            "Apply confirmation",
            "FAIL",
            "Apply mode requires --confirm-account-id to match the current AWS account "
            f"({caller_account_id}).",
            blocker=True,
        )

    if delegated_admin_account_id and (
        confirm_delegated_admin_account_id != delegated_admin_account_id
    ):
        return CheckResult(
            "Delegated admin confirmation",
            "FAIL",
            "Delegated admin registration requires --confirm-delegated-admin-account-id "
            f"to match {delegated_admin_account_id}.",
            blocker=True,
        )

    return CheckResult(
        "Apply confirmation",
        "OK",
        f"Confirmed apply target account {caller_account_id}.",
    )


def print_result(result: CheckResult) -> None:
    """Print a human-readable check result."""
    icons = {
        "OK": "OK",
        "APPLY": "APPLY",
        "MANUAL": "MANUAL",
        "WARN": "WARN",
        "FAIL": "FAIL",
        "SKIP": "SKIP",
    }
    print(f"[{icons.get(result.status, result.status)}] {result.name}: {result.detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check and optionally bootstrap MPPO prerequisite services."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check prerequisites only.")
    mode.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply prerequisite changes. Requires --confirm-account-id because "
            "changes can affect the whole AWS Organization."
        ),
    )
    parser.add_argument("--region", default="us-east-1", help="AWS region. Defaults to us-east-1.")
    parser.add_argument(
        "--confirm-account-id",
        default=None,
        help="Required with --apply. Must match the current AWS account ID.",
    )
    parser.add_argument(
        "--delegated-admin-account-id",
        default=None,
        help="Optional account ID to register as License Manager delegated admin.",
    )
    parser.add_argument(
        "--confirm-delegated-admin-account-id",
        default=None,
        help=(
            "Required with --apply and --delegated-admin-account-id. Must match "
            "the delegated admin account ID."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    apply_changes = args.apply

    sts = boto3.client("sts", region_name=args.region)
    orgs = boto3.client("organizations", region_name=args.region)
    lm = boto3.client("license-manager", region_name=args.region)
    iam = boto3.client("iam", region_name=args.region)

    print("=" * 72)
    print("MPPO prerequisite bootstrap")
    print("=" * 72)
    print(f"Region: {args.region}")
    print(f"Mode: {'apply' if apply_changes else 'check'}")
    print()

    try:
        caller_account_id = get_caller_account_id(sts)
    except ClientError as error:
        result = CheckResult("AWS credentials", "FAIL", str(error), blocker=True)
        print_result(result)
        return 1

    print(f"Account: {caller_account_id}")
    print()

    results: list[CheckResult] = []
    confirmation_result = validate_apply_confirmation(
        apply_changes=apply_changes,
        caller_account_id=caller_account_id,
        confirm_account_id=args.confirm_account_id,
        delegated_admin_account_id=args.delegated_admin_account_id,
        confirm_delegated_admin_account_id=args.confirm_delegated_admin_account_id,
    )
    results.append(confirmation_result)
    if apply_changes:
        print_result(confirmation_result)
        if confirmation_result.blocker:
            print()
            print("Result: prerequisites need attention.")
            print(f"- {confirmation_result.name}: {confirmation_result.detail}")
            return 1

    org_result = check_organization(orgs, caller_account_id)
    results.append(org_result)
    print_result(org_result)
    can_apply_org_prereqs = apply_changes and not org_result.blocker

    lm_result = check_license_manager_settings(lm)
    if can_apply_org_prereqs and lm_result.status == "APPLY":
        lm_result = apply_license_manager_settings(lm)
    results.append(lm_result)
    print_result(lm_result)

    trusted_result = check_marketplace_trusted_access(orgs)
    if can_apply_org_prereqs and trusted_result.status == "APPLY":
        trusted_result = apply_marketplace_trusted_access(orgs)
    results.append(trusted_result)
    print_result(trusted_result)

    lm_trusted_result = check_license_manager_trusted_access(orgs)
    if can_apply_org_prereqs and lm_trusted_result.status == "APPLY":
        lm_trusted_result = apply_license_manager_trusted_access(orgs)
    results.append(lm_trusted_result)
    print_result(lm_trusted_result)

    roles_result = check_service_linked_roles(iam)
    results.append(roles_result)
    print_result(roles_result)

    delegated_result = check_delegated_admin(orgs, args.delegated_admin_account_id)
    if can_apply_org_prereqs and delegated_result.status == "APPLY":
        delegated_result = apply_delegated_admin(orgs, args.delegated_admin_account_id)
    results.append(delegated_result)
    print_result(delegated_result)

    print()
    blockers = [result for result in results if result.blocker]
    if blockers:
        print("Result: prerequisites need attention.")
        for result in blockers:
            print(f"- {result.name}: {result.detail}")
        return 1

    print("Result: prerequisites are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
