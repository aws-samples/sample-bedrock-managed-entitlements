"""Step 3: Create and activate grants via License Manager.

Supports three grant target types:
- Organization (default): distributes to all accounts in the org
- Organizational Unit (OU): distributes to accounts within specific OUs
- Account: targets individual AWS account IDs

API Reference:
- CreateGrant: requires ClientToken, GrantName, LicenseArn, Principals, HomeRegion, AllowedOperations
- CreateGrantVersion: requires ClientToken, GrantArn; optional Status, SourceVersion
- Principals: array of exactly 1 ARN (account, OU, or organization)
  - Organization: arn:aws:organizations::<mgmt-account>:organization/<org-id>
  - OU: arn:aws:organizations::<mgmt-account>:ou/<org-id>/<ou-id>
  - Account: arn:aws:iam::<account-id>:root

Note: CreateGrant accepts exactly 1 principal per call. For multiple OUs or accounts,
this function creates multiple grants (one per target).
"""

import logging
import time
import uuid

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Grant target types
TARGET_ORGANIZATION = "organization"
TARGET_OU = "ou"
TARGET_ACCOUNT = "account"


def create_and_activate_grants(
    license_arn: str,
    grant_targets: list[dict],
    seller_name: str,
    product_name: str,
    home_region: str = "us-east-1",
    auto_activate: bool = True,
    replace_legacy_grants: bool = False,
) -> list[dict]:
    """Create and activate grants for one or more targets.

    This is the multi-target entry point. For backward compatibility,
    create_and_activate_grant() (singular) still works for org-wide grants.

    Args:
        license_arn: ARN of the license to grant
        grant_targets: List of target dicts, each with:
            - type: "organization" | "ou" | "account"
            - id: org ID, OU ID, or account ID
        seller_name: Human-readable seller name (for grant naming)
        product_name: Product name (for grant naming)
        home_region: License home region (always us-east-1 for Marketplace)
        auto_activate: Whether to immediately activate grants
        replace_legacy_grants: If True, uses ALL_GRANTS_PERMITTED_BY_ISSUER

    Returns:
        List of result dicts, one per target
    """
    results = []
    for target in grant_targets:
        result = _create_single_grant(
            license_arn=license_arn,
            target=target,
            seller_name=seller_name,
            product_name=product_name,
            home_region=home_region,
            auto_activate=auto_activate,
            replace_legacy_grants=replace_legacy_grants,
        )
        results.append(result)
    return results


def create_and_activate_grant(
    license_arn: str,
    organization_id: str,
    seller_name: str,
    product_name: str,
    home_region: str = "us-east-1",
    auto_activate: bool = True,
    replace_legacy_grants: bool = False,
    grant_targets: list[dict] | None = None,
) -> dict:
    """Create and activate grant(s). Backward-compatible single-grant entry point.

    If grant_targets is provided, creates grants for each target.
    Otherwise, defaults to a single organization-wide grant using organization_id.

    Args:
        license_arn: ARN of the license to grant
        organization_id: AWS Organization ID (default target for org-wide grant)
        seller_name: Human-readable seller name
        product_name: Product name
        home_region: License home region (always us-east-1)
        auto_activate: Whether to immediately activate
        replace_legacy_grants: Use ALL_GRANTS_PERMITTED_BY_ISSUER
        grant_targets: Optional list of targets. Overrides organization_id if provided.

    Returns:
        Dict with grant results (single grant returns flat dict for backward compat)
    """
    if grant_targets:
        results = create_and_activate_grants(
            license_arn=license_arn,
            grant_targets=grant_targets,
            seller_name=seller_name,
            product_name=product_name,
            home_region=home_region,
            auto_activate=auto_activate,
            replace_legacy_grants=replace_legacy_grants,
        )
        # Return summary
        return {
            "grant_arn": results[0]["grant_arn"] if results else None,
            "status": results[0]["status"] if results else "no_targets",
            "organization_id": organization_id,
            "license_arn": license_arn,
            "grants": results,
            "grant_count": len(results),
        }
    else:
        # Default: single org-wide grant (backward compatible)
        target = {"type": TARGET_ORGANIZATION, "id": organization_id}
        result = _create_single_grant(
            license_arn=license_arn,
            target=target,
            seller_name=seller_name,
            product_name=product_name,
            home_region=home_region,
            auto_activate=auto_activate,
            replace_legacy_grants=replace_legacy_grants,
        )
        return {
            "grant_arn": result["grant_arn"],
            "status": result["status"],
            "organization_id": organization_id,
            "license_arn": license_arn,
        }


def _build_principal_arn(target: dict, account_id: str) -> str:
    """Build the principal ARN for a grant target.

    Args:
        target: dict with 'type' and 'id'
        account_id: Management account ID (needed for org/OU ARN construction)

    Returns:
        Principal ARN string

    Principal ARN formats:
        Organization: arn:aws:organizations::<mgmt-account>:organization/<org-id>
        OU:           arn:aws:organizations::<mgmt-account>:ou/<org-id>/<ou-id>
        Account:      arn:aws:iam::<account-id>:root
    """
    target_type = target["type"]
    target_id = target["id"]

    if target_type == TARGET_ORGANIZATION:
        return f"arn:aws:organizations::{account_id}:organization/{target_id}"
    elif target_type == TARGET_OU:
        # OU ARN requires the org ID prefix. The target id can be either:
        # - Full: "o-abc123/ou-abc1-23456789" (org-id/ou-id)
        # - Short: "ou-abc1-23456789" (just the OU ID, we'll need org context)
        if "/" in target_id:
            # Full format: org-id/ou-id
            return f"arn:aws:organizations::{account_id}:ou/{target_id}"
        else:
            # Just the OU ID — caller should provide the full path
            # but we'll construct it as best we can
            return f"arn:aws:organizations::{account_id}:ou/{target_id}"
    elif target_type == TARGET_ACCOUNT:
        return f"arn:aws:iam::{target_id}:root"
    else:
        raise ValueError(f"Unknown grant target type: {target_type}")


def _build_grant_name(seller_name: str, product_name: str, target: dict) -> str:
    """Generate a descriptive grant name based on the target type."""
    target_type = target["type"]
    target_id = target["id"]

    if target_type == TARGET_ORGANIZATION:
        suffix = "org"
    elif target_type == TARGET_OU:
        # Use last segment of OU ID for brevity
        ou_short = target_id.split("/")[-1] if "/" in target_id else target_id
        suffix = f"ou-{ou_short}"
    elif target_type == TARGET_ACCOUNT:
        suffix = f"acct-{target_id}"
    else:
        suffix = "grant"

    return f"{seller_name}-{product_name}-{suffix}"[:256]


def _create_single_grant(
    license_arn: str,
    target: dict,
    seller_name: str,
    product_name: str,
    home_region: str = "us-east-1",
    auto_activate: bool = True,
    replace_legacy_grants: bool = False,
) -> dict:
    """Create and activate a single grant for one target.

    Args:
        license_arn: ARN of the license to grant
        target: dict with 'type' ("organization"|"ou"|"account") and 'id'
        seller_name: Human-readable seller name
        product_name: Product name
        home_region: License home region
        auto_activate: Whether to immediately activate
        replace_legacy_grants: Use ALL_GRANTS_PERMITTED_BY_ISSUER

    Returns:
        Dict with grant_arn, status, target_type, target_id
    """
    license_manager = boto3.client("license-manager", region_name=home_region)
    sts = boto3.client("sts")

    account_id = sts.get_caller_identity()["Account"]
    principal_arn = _build_principal_arn(target, account_id)
    grant_name = _build_grant_name(seller_name, product_name, target)
    client_token = str(uuid.uuid4())

    logger.info(
        "Creating grant: name=%s, license=%s, principal=%s, target_type=%s",
        grant_name, license_arn, principal_arn, target["type"],
    )

    # Create the grant
    try:
        create_response = license_manager.create_grant(
            ClientToken=client_token,
            GrantName=grant_name,
            LicenseArn=license_arn,
            Principals=[principal_arn],
            HomeRegion=home_region,
            AllowedOperations=[
                "CheckoutLicense",
                "CheckInLicense",
                "ExtendConsumptionLicense",
                "ListPurchasedLicenses",
                "CreateToken",
            ],
        )
        grant_arn = create_response["GrantArn"]
        grant_status = create_response.get("Status", "UNKNOWN")
        grant_version = create_response.get("Version", "1")
        logger.info(
            "Grant created: arn=%s, status=%s, version=%s",
            grant_arn, grant_status, grant_version,
        )

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ValidationException" and "already exists" in str(e).lower():
            logger.info("Grant may already exist, searching for it...")
            grant_arn = _find_existing_grant(
                license_manager, license_arn, principal_arn
            )
            if not grant_arn:
                raise ValueError(
                    f"Grant reportedly exists but could not be found for "
                    f"license {license_arn}, principal {principal_arn}"
                )
            grant_status = "EXISTING"
            grant_version = None
        else:
            raise

    # Activate the grant if requested
    if auto_activate:
        activation_status = _activate_grant(
            license_manager, grant_arn, grant_version,
            replace_legacy=replace_legacy_grants,
        )
    else:
        activation_status = grant_status

    return {
        "grant_arn": grant_arn,
        "status": activation_status,
        "target_type": target["type"],
        "target_id": target["id"],
        "principal_arn": principal_arn,
        "license_arn": license_arn,
    }


def _activate_grant(
    client,
    grant_arn: str,
    source_version: str | None = None,
    max_retries: int = 6,
    poll_interval_seconds: float = 5.0,
    replace_legacy: bool = False,
) -> str:
    """Activate a grant by calling CreateGrantVersion with Status=ACTIVE.

    Grant lifecycle:
    - After CreateGrant: PENDING_WORKFLOW -> DISABLED (for org grants, auto-accepted)
    - After CreateGrantVersion(ACTIVE): PENDING_WORKFLOW -> WORKFLOW_COMPLETED/ACTIVE

    For org/OU grants, grants auto-accept and land in DISABLED.
    For account grants, the recipient must accept first (PENDING_ACCEPT → DISABLED → ACTIVE).

    The ActivationOverrideBehavior option controls how this interacts with
    other existing grants for the same product:
    - DISTRIBUTED_GRANTS_ONLY: Activate without affecting other grants (default)
    - ALL_GRANTS_PERMITTED_BY_ISSUER: Replace existing per-account grants for
      the same product with this grant (use for legacy cleanup)

    Args:
        client: License Manager boto3 client
        grant_arn: ARN of the grant to activate
        source_version: Current version of the grant (optional)
        max_retries: Maximum retry attempts
        poll_interval_seconds: Seconds to wait between status checks
        replace_legacy: If True, use ALL_GRANTS_PERMITTED_BY_ISSUER

    Returns:
        Final grant status string
    """
    activation_requested = False
    last_status = "UNKNOWN"

    for attempt in range(max_retries):
        try:
            # Check current status
            grant_response = client.get_grant(GrantArn=grant_arn)
            grant = grant_response["Grant"]
            current_status = grant.get("GrantStatus", "UNKNOWN")
            current_version = grant.get("Version", "1")
            last_status = current_status
            logger.info(
                "Grant %s current status: %s (version: %s, attempt: %d/%d)",
                grant_arn, current_status, current_version, attempt + 1, max_retries,
            )

            if current_status == "ACTIVE":
                logger.info("Grant already active")
                return "ACTIVE"

            if current_status == "WORKFLOW_COMPLETED":
                logger.info("Grant workflow completed (effectively active)")
                return "WORKFLOW_COMPLETED"

            if current_status in ("PENDING_WORKFLOW", "PENDING_ACCEPT"):
                # Grant is still being processed, wait
                logger.info("Grant still processing (status: %s), waiting...", current_status)
                time.sleep(poll_interval_seconds)
                continue

            # Status is DISABLED — activate it
            if current_status == "DISABLED":
                if activation_requested:
                    logger.info("Grant activation already requested, waiting for workflow")
                    time.sleep(poll_interval_seconds)
                    continue

                override_behavior = (
                    "ALL_GRANTS_PERMITTED_BY_ISSUER"
                    if replace_legacy
                    else "DISTRIBUTED_GRANTS_ONLY"
                )
                logger.info(
                    "Grant is DISABLED, activating with behavior: %s",
                    override_behavior,
                )
                activate_response = client.create_grant_version(
                    ClientToken=str(uuid.uuid4()),
                    GrantArn=grant_arn,
                    Status="ACTIVE",
                    SourceVersion=current_version,
                    Options={
                        "ActivationOverrideBehavior": override_behavior
                    },
                )
                activation_requested = True
                new_status = activate_response.get("Status", "UNKNOWN")
                logger.info(
                    "Activation response: status=%s, version=%s",
                    new_status, activate_response.get("Version"),
                )

                if new_status in ("ACTIVE", "WORKFLOW_COMPLETED"):
                    return new_status

                time.sleep(poll_interval_seconds)
                continue

            # Unexpected status
            logger.warning(
                "Unexpected grant status: %s (attempt %d/%d)",
                current_status, attempt + 1, max_retries,
            )
            time.sleep(poll_interval_seconds)

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in (
                "InvalidParameterValueException",
                "ValidationException",
            ):
                logger.warning(
                    "Activation attempt %d failed: %s - %s",
                    attempt + 1, error_code,
                    e.response["Error"]["Message"],
                )
                time.sleep(poll_interval_seconds)
            else:
                raise

    logger.warning(
        "Grant activation did not complete after %d attempts; last status: %s",
        max_retries, last_status,
    )
    return "ACTIVATION_PENDING"


def _find_existing_grant(
    client,
    license_arn: str,
    principal_arn: str,
) -> str | None:
    """Find an existing grant for the given license and principal."""
    try:
        response = client.list_distributed_grants(
            Filters=[
                {"Name": "LicenseArn", "Values": [license_arn]},
                {"Name": "GranteePrincipalARN", "Values": [principal_arn]},
            ]
        )
        grants = response.get("Grants", [])
        if grants:
            return grants[0]["GrantArn"]
    except ClientError as e:
        logger.warning("Error searching for existing grant: %s", str(e))

    return None
