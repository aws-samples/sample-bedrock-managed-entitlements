"""Reconcile offers and grant activation, notifying only when action is needed."""

import logging

import boto3

from auto_accept_handler import _list_private_offers
from steps.create_grant import _build_principal_arn
from steps.notify import notify_admins

logger = logging.getLogger(__name__)

ACTIVE_GRANT_STATUSES = {"ACTIVE", "WORKFLOW_COMPLETED"}
IGNORED_LICENSE_STATUSES = {"EXPIRED", "DELETED"}


def monitor_entitlements(
    seller_table_name: str,
    pending_table_name: str,
    organization_id: str,
    home_region: str,
    topic_arn: str,
    deployment_name: str,
) -> dict:
    """Find offers awaiting acceptance and licenses without active grants."""
    dynamodb = boto3.resource("dynamodb", region_name=home_region)
    sellers = _scan_all(dynamodb.Table(seller_table_name))
    pending = _scan_all(dynamodb.Table(pending_table_name))

    discovery = boto3.client("marketplace-discovery", region_name=home_region)
    license_manager = boto3.client("license-manager", region_name=home_region)
    account_id = boto3.client("sts", region_name=home_region).get_caller_identity()[
        "Account"
    ]

    profile_ids = [
        seller["sellerProfileId"]
        for seller in sellers
        if seller.get("sellerProfileId")
    ]
    offers = _list_private_offers(discovery, profile_ids) if profile_ids else []
    licenses = _list_received_licenses(license_manager)

    issues = _find_issues(
        sellers=sellers,
        offers=offers,
        licenses=licenses,
        pending=pending,
        license_manager=license_manager,
        organization_id=organization_id,
        account_id=account_id,
    )

    if issues:
        notify_admins(
            topic_arn=topic_arn,
            subject=f"MPPO Entitlement Monitor: {len(issues)} Issue(s)",
            message="\n".join(
                [
                    "The entitlement reconciliation found items that need attention:",
                    "",
                    *(f"  - {issue}" for issue in issues),
                    "",
                    "Review the offers and grants in the AWS Marketplace and "
                    "License Manager consoles.",
                ]
            ),
        )
    else:
        logger.info("Entitlement reconciliation completed with no issues")

    _emit_heartbeat(home_region, deployment_name)

    return {
        "status": "issues_found" if issues else "healthy",
        "sellersChecked": len(sellers),
        "offersPending": len(offers),
        "pendingActivations": len(pending),
        "issues": len(issues),
    }


def _emit_heartbeat(home_region: str, deployment_name: str) -> None:
    """Record successful completion so a dead-man alarm can detect missed runs."""
    boto3.client("cloudwatch", region_name=home_region).put_metric_data(
        Namespace="MPPO/Entitlements",
        MetricData=[
            {
                "MetricName": "ReconciliationCompleted",
                "Dimensions": [
                    {"Name": "Deployment", "Value": deployment_name},
                ],
                "Value": 1,
                "Unit": "Count",
            }
        ],
    )


def _find_issues(
    sellers: list[dict],
    offers: list[dict],
    licenses: list[dict],
    pending: list[dict],
    license_manager,
    organization_id: str,
    account_id: str,
) -> list[str]:
    """Return human-readable reconciliation issues."""
    issues = []
    sellers_by_profile = {
        seller.get("sellerProfileId"): seller
        for seller in sellers
        if seller.get("sellerProfileId")
    }

    for offer in offers:
        seller = sellers_by_profile.get(offer.get("seller_profile_id"), {})
        issues.append(
            f"offer {offer.get('offer_id', 'unknown')} from "
            f"{seller.get('name', offer.get('seller_name', 'unknown seller'))} "
            "has not been accepted"
        )

    for item in pending:
        issues.append(
            f"grant {item.get('grantArn', 'unknown')} for "
            f"{item.get('sellerName', 'unknown seller')} / "
            f"{item.get('productName', 'unknown product')} is not active "
            f"(last status: {item.get('lastStatus', 'UNKNOWN')}, "
            f"retries: {item.get('retryCount', 0)})"
        )

    for seller in sellers:
        seller_name = seller.get("name", seller.get("proposerAccountId", "unknown"))
        issuer_name = seller.get("issuerName")
        product_skus = set(seller.get("productSkus", []))
        product_names = set(seller.get("productNames", []))

        if not seller.get("sellerProfileId"):
            issues.append(
                f"{seller_name} cannot be monitored for unaccepted offers because "
                "sellerProfileId is missing"
            )

        if not issuer_name or not (product_skus or product_names):
            issues.append(
                f"{seller_name} cannot be safely reconciled to licenses; configure "
                "issuerName and productSkus or productNames"
            )
            continue

        targets = seller.get("grantTargets") or [
            {"type": "organization", "id": organization_id}
        ]
        matching_licenses = [
            license_record
            for license_record in licenses
            if _license_matches(
                license_record, issuer_name, product_skus, product_names
            )
        ]
        for license_record in matching_licenses:
            license_arn = license_record.get("LicenseArn")
            for target in targets:
                principal_arn = _build_principal_arn(target, account_id)
                grant = _find_grant(
                    license_manager, license_arn, principal_arn
                )
                if not grant:
                    issues.append(
                        f"license {license_arn} for {seller_name} has no grant "
                        f"for {target.get('type')} {target.get('id')}"
                    )
                elif grant.get("GrantStatus") not in ACTIVE_GRANT_STATUSES:
                    issues.append(
                        f"grant {grant.get('GrantArn', 'unknown')} for "
                        f"{seller_name} is {grant.get('GrantStatus', 'UNKNOWN')}"
                    )
    return issues


def _license_matches(
    license_record: dict,
    issuer_name: str,
    product_skus: set[str],
    product_names: set[str],
) -> bool:
    if license_record.get("Status") in IGNORED_LICENSE_STATUSES:
        return False
    if (license_record.get("Issuer") or {}).get("Name") != issuer_name:
        return False
    return (
        license_record.get("ProductSKU") in product_skus
        or license_record.get("ProductName") in product_names
    )


def _find_grant(license_manager, license_arn: str, principal_arn: str) -> dict | None:
    filters = [
        {"Name": "LicenseArn", "Values": [license_arn]},
        {"Name": "GranteePrincipalARN", "Values": [principal_arn]},
    ]
    token = None
    while True:
        kwargs = {"Filters": filters}
        if token:
            kwargs["NextToken"] = token
        response = license_manager.list_distributed_grants(**kwargs)
        grants = response.get("Grants", [])
        if grants:
            return grants[0]
        token = response.get("NextToken")
        if not token:
            return None


def _list_received_licenses(license_manager) -> list[dict]:
    licenses = []
    paginator = license_manager.get_paginator("list_received_licenses")
    for page in paginator.paginate():
        licenses.extend(page.get("Licenses", []))
    return licenses


def _scan_all(table) -> list[dict]:
    items = []
    kwargs = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key
