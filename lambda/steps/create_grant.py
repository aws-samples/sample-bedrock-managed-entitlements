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
import re
import time
import uuid

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Grant target types
TARGET_ORGANIZATION = "organization"
TARGET_OU = "ou"
TARGET_ACCOUNT = "account"

_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
_ORG_ID_RE = re.compile(r"^o-[a-z0-9]{10,32}$")
_OU_ID_RE = re.compile(r"^(o-[a-z0-9]{10,32}/)?ou-[a-z0-9]{4,32}-[a-z0-9]{8,32}$")
DEFAULT_ALLOWED_OPERATIONS = [
    "CheckoutLicense",
    "CheckInLicense",
    "ExtendConsumptionLicense",
    "ListPurchasedLicenses",
    "CreateToken",
]
_DUPLICATE_GRANT_HINTS = (
    "already has a grant",
    "already distributed",
    "already exist",
    "already exists",
    "conflict",
    "duplicate",
)


def create_and_activate_grants(
    license_arn: str,
    grant_targets: list[dict],
    seller_name: str,
    product_name: str,
    organization_id: str,
    home_region: str = "us-east-1",
    auto_activate: bool = True,
    replace_legacy_grants: bool = False,
    allowed_operations: list[str] | None = None,
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
        organization_id: This deployment's AWS Organization ID. Every target
            is validated to belong to this org (or equal it, for org-level
            targets) before a grant is created — see _validate_target.
        home_region: License home region (always us-east-1 for Marketplace)
        auto_activate: Whether to immediately activate grants
        replace_legacy_grants: If True, uses ALL_GRANTS_PERMITTED_BY_ISSUER
        allowed_operations: Optional operations to grant. If omitted, operations
            are derived from the parent grant when available, then fall back to
            the Bedrock default operation set.

    Returns:
        List of result dicts, one per target
    """
    results = []
    for target in grant_targets:
        _validate_target(target, organization_id)
        result = _create_single_grant(
            license_arn=license_arn,
            target=target,
            seller_name=seller_name,
            product_name=product_name,
            home_region=home_region,
            auto_activate=auto_activate,
            replace_legacy_grants=replace_legacy_grants,
            allowed_operations=allowed_operations,
        )
        results.append(result)
    return results


def _validate_target(target: dict, organization_id: str) -> None:
    """Validate a grant target's shape and organization membership.

    Raises ValueError on any malformed ID or an org/OU target that does not
    belong to this deployment's organization. This is the only guard
    available: License Manager's CreateGrant API exposes no grantee
    condition key, so a bad principal ARN would otherwise reach the API
    unrejected.
    """
    target_type = target.get("type")
    target_id = str(target.get("id", ""))

    if target_type == TARGET_ACCOUNT:
        if not _ACCOUNT_ID_RE.match(target_id):
            raise ValueError(
                f"Invalid account ID grant target: {target_id!r} "
                f"(expected 12 digits)"
            )
        _verify_account_in_organization(target_id, organization_id)
    elif target_type == TARGET_OU:
        ou_only = target_id.rsplit("/", 1)[-1]
        if not _OU_ID_RE.match(target_id) and not re.match(r"^ou-[a-z0-9]{4,32}-[a-z0-9]{8,32}$", ou_only):
            raise ValueError(
                f"Invalid OU grant target: {target_id!r}"
            )
        target_org = target_id.split("/")[0] if "/" in target_id else organization_id
        if target_org != organization_id:
            raise ValueError(
                f"OU grant target {target_id!r} belongs to organization "
                f"{target_org!r}, expected {organization_id!r}"
            )
    elif target_type == TARGET_ORGANIZATION:
        if target_id != organization_id:
            raise ValueError(
                f"Organization grant target {target_id!r} does not match "
                f"this deployment's organization {organization_id!r}"
            )
    else:
        raise ValueError(f"Unknown grant target type: {target_type!r}")


def _verify_account_in_organization(account_id: str, organization_id: str) -> None:
    """Verify an account ID is actually a member of this organization.

    Uses ListAccounts (paginated) rather than DescribeOrganization, which
    only describes the org itself and cannot confirm membership.
    """
    orgs = boto3.client("organizations")
    paginator = orgs.get_paginator("list_accounts")
    for page in paginator.paginate():
        for acct in page.get("Accounts", []):
            if acct.get("Id") == account_id:
                return
    raise ValueError(
        f"Account {account_id!r} is not a member of organization "
        f"{organization_id!r} — refusing to create a grant for it"
    )


def create_and_activate_grant(
    license_arn: str,
    organization_id: str,
    seller_name: str,
    product_name: str,
    home_region: str = "us-east-1",
    auto_activate: bool = True,
    replace_legacy_grants: bool = False,
    grant_targets: list[dict] | None = None,
    allowed_operations: list[str] | None = None,
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
        allowed_operations: Optional operations to grant. If omitted, operations
            are derived from the parent grant when available.

    Returns:
        Dict with grant results (single grant returns flat dict for backward compat)
    """
    if grant_targets:
        results = create_and_activate_grants(
            license_arn=license_arn,
            grant_targets=grant_targets,
            seller_name=seller_name,
            product_name=product_name,
            organization_id=organization_id,
            home_region=home_region,
            auto_activate=auto_activate,
            replace_legacy_grants=replace_legacy_grants,
            allowed_operations=allowed_operations,
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
            allowed_operations=allowed_operations,
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
    allowed_operations: list[str] | None = None,
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
        allowed_operations: Optional operations to grant. If omitted, operations
            are derived from the parent grant when available.

    Returns:
        Dict with grant_arn, status, target_type, target_id
    """
    license_manager = boto3.client("license-manager", region_name=home_region)
    sts = boto3.client("sts")

    account_id = sts.get_caller_identity()["Account"]
    principal_arn = _build_principal_arn(target, account_id)
    grant_name = _build_grant_name(seller_name, product_name, target)
    client_token = str(uuid.uuid4())
    operations = _resolve_allowed_operations(
        license_manager,
        license_arn,
        allowed_operations,
    )

    logger.info(
        "Creating grant: name=%s, license=%s, principal=%s, "
        "target_type=%s, operations=%s",
        grant_name, license_arn, principal_arn, target["type"], operations,
    )

    # Create the grant
    try:
        create_response = license_manager.create_grant(
            ClientToken=client_token,
            GrantName=grant_name,
            LicenseArn=license_arn,
            Principals=[principal_arn],
            HomeRegion=home_region,
            AllowedOperations=operations,
        )
        grant_arn = create_response["GrantArn"]
        grant_status = create_response.get("Status", "UNKNOWN")
        grant_version = create_response.get("Version", "1")
        logger.info(
            "Grant created: arn=%s, status=%s, version=%s",
            grant_arn, grant_status, grant_version,
        )

    except ClientError as e:
        if _is_duplicate_grant_error(e):
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
        token = None
        while True:
            kwargs = {
                "Filters": [
                    {"Name": "LicenseArn", "Values": [license_arn]},
                    {"Name": "GranteePrincipalARN", "Values": [principal_arn]},
                ]
            }
            if token:
                kwargs["NextToken"] = token
            response = client.list_distributed_grants(**kwargs)
            grants = response.get("Grants", [])
            if grants:
                return grants[0]["GrantArn"]
            token = response.get("NextToken")
            if not token:
                return None
    except ClientError as e:
        logger.warning("Error searching for existing grant: %s", str(e))

    return None


def _resolve_allowed_operations(
    client,
    license_arn: str,
    allowed_operations: list[str] | None = None,
) -> list[str]:
    """Return grant operations from explicit config, parent grant, or defaults."""
    if allowed_operations:
        return _normalise_allowed_operations(allowed_operations)

    parent_operations = _derive_allowed_operations_from_parent(client, license_arn)
    if parent_operations:
        return parent_operations

    logger.warning(
        "Could not derive allowed operations from parent grant for license %s; "
        "using default Bedrock operation set",
        license_arn,
    )
    return list(DEFAULT_ALLOWED_OPERATIONS)


def _derive_allowed_operations_from_parent(client, license_arn: str) -> list[str]:
    """Derive distributable operations from the grantor's parent grant."""
    try:
        license_record = _find_received_license(client, license_arn)
        if not license_record:
            return []
        parent_grant_arn = _parent_grant_arn_from_license(license_record)
        if not parent_grant_arn:
            return []
        parent_grant = client.get_grant(GrantArn=parent_grant_arn)["Grant"]
        operations = (
            parent_grant.get("AllowedOperations")
            or parent_grant.get("GrantedOperations")
            or []
        )
        return _normalise_allowed_operations(operations)
    except ClientError as e:
        logger.warning(
            "Error deriving allowed operations from parent grant for %s: %s",
            license_arn, str(e),
        )
        return []


def _find_received_license(client, license_arn: str) -> dict | None:
    """Find a received license record by ARN."""
    token = None
    while True:
        kwargs = {"MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        response = client.list_received_licenses(**kwargs)
        for license_record in response.get("Licenses", []):
            if license_record.get("LicenseArn") == license_arn:
                return license_record
        token = response.get("NextToken")
        if not token:
            return None


def _parent_grant_arn_from_license(license_record: dict) -> str | None:
    """Extract the parent grant ARN from LicenseMetadata."""
    for metadata in license_record.get("LicenseMetadata", []):
        if metadata.get("Name") == "grantArn":
            return metadata.get("Value")
    return None


def _normalise_allowed_operations(operations: list[str]) -> list[str]:
    """Remove CreateGrant and duplicates while preserving operation order."""
    normalised = []
    seen = set()
    for operation in operations:
        if operation == "CreateGrant" or operation in seen:
            continue
        normalised.append(operation)
        seen.add(operation)
    return normalised


def _is_duplicate_grant_error(error: ClientError) -> bool:
    """Return True when License Manager reports an already-created grant."""
    error_data = error.response.get("Error", {})
    code = error_data.get("Code", "")
    message = error_data.get("Message", "")
    blob = f"{code} {message}".lower()
    return any(hint in blob for hint in _DUPLICATE_GRANT_HINTS)
