#!/usr/bin/env python3
"""Backfill grants for existing Marketplace licenses in a controlled way.

Default mode is dry-run. Apply mode requires explicit account confirmation:

    python3 scripts/backfill_grants.py --config config/sellers.json
    python3 scripts/backfill_grants.py --config config/sellers.json \
        --license-arn arn:aws:license-manager::123456789012:license:l-example \
        --apply --confirm-account-id 123456789012

This script is intentionally not a "distribute every license" helper. License
Manager received licenses do not reliably expose the Marketplace proposer
account ID, so broad issuer-only matches are blocked unless the operator passes
explicit license ARNs or narrows the seller config with product filters.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from steps.create_grant import create_and_activate_grants  # noqa: E402
from steps.pending_grants import SUCCESS_STATUSES, record_pending_grant  # noqa: E402


IGNORED_LICENSE_STATES = {"EXPIRED", "DELETED"}
DEFAULT_PENDING_GRANT_TABLE_NAME = "mppo-pending-grants"


@dataclass(frozen=True)
class BackfillPlanItem:
    seller: dict[str, Any]
    license_record: dict[str, Any]
    grant_targets: list[dict[str, str]]


@dataclass(frozen=True)
class BackfillPlan:
    items: list[BackfillPlanItem]
    skipped: list[str]
    blocked: list[str]


def load_config(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def list_received_licenses(client) -> list[dict[str, Any]]:
    licenses = []
    token = None
    while True:
        kwargs = {"MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        response = client.list_received_licenses(**kwargs)
        licenses.extend(response.get("Licenses", []))
        token = response.get("NextToken")
        if not token:
            return licenses


def product_filters(seller: dict[str, Any]) -> tuple[set[str], set[str]]:
    return (
        set(seller.get("productSkus", [])),
        set(seller.get("productNames", [])),
    )


def has_product_filters(seller: dict[str, Any]) -> bool:
    skus, names = product_filters(seller)
    return bool(skus or names)


def license_matches_seller(
    license_record: dict[str, Any],
    seller: dict[str, Any],
    explicit_license_arns: set[str],
) -> bool:
    if license_record.get("Status") in IGNORED_LICENSE_STATES:
        return False

    issuer_name = license_record.get("Issuer", {}).get("Name")
    if issuer_name != seller.get("issuerName"):
        return False

    if license_record.get("LicenseArn") in explicit_license_arns:
        return True

    skus, names = product_filters(seller)
    if skus and license_record.get("ProductSKU") in skus:
        return True
    if names and license_record.get("ProductName") in names:
        return True

    return False


def build_backfill_plan(
    config: dict[str, Any],
    licenses: list[dict[str, Any]],
    license_arns: list[str] | None = None,
    seller_accounts: list[str] | None = None,
) -> BackfillPlan:
    org_id = config.get("organizationId")
    explicit_license_arns = set(license_arns or [])
    selected_sellers = set(seller_accounts or [])

    items: list[BackfillPlanItem] = []
    skipped: list[str] = []
    blocked: list[str] = []

    for seller in config.get("allowedSellers", []):
        seller_account = seller.get("proposerAccountId")
        seller_name = seller.get("name", seller_account)
        if selected_sellers and seller_account not in selected_sellers:
            continue

        if not seller.get("issuerName"):
            blocked.append(f"{seller_name}: missing issuerName")
            continue

        if not has_product_filters(seller) and not explicit_license_arns:
            blocked.append(
                f"{seller_name}: issuer-only match is too broad; add productSkus, "
                "productNames, or pass --license-arn"
            )
            continue

        matches = [
            license_record for license_record in licenses
            if license_matches_seller(license_record, seller, explicit_license_arns)
        ]

        if not matches:
            skipped.append(f"{seller_name}: no matching received licenses")
            continue

        grant_targets = seller.get("grantTargets") or [
            {"type": "organization", "id": org_id}
        ]
        for license_record in matches:
            items.append(BackfillPlanItem(seller, license_record, grant_targets))

    return BackfillPlan(items=items, skipped=skipped, blocked=blocked)


def validate_apply_context(sts_client, orgs_client, config: dict[str, Any], confirm_account_id: str | None) -> int:
    caller_account = sts_client.get_caller_identity()["Account"]
    if confirm_account_id != caller_account:
        print(
            "Apply mode requires --confirm-account-id to match the current "
            f"AWS account ({caller_account})."
        )
        return 1

    actual_org = orgs_client.describe_organization()["Organization"]["Id"]
    expected_org = config.get("organizationId")
    if actual_org != expected_org:
        print(f"Config organizationId {expected_org} does not match actual org {actual_org}.")
        return 1

    return 0


def print_plan(plan: BackfillPlan, apply: bool) -> None:
    print(f"Mode: {'apply' if apply else 'dry-run'}")
    print()
    if plan.items:
        print("Planned grants:")
        for item in plan.items:
            seller = item.seller
            license_record = item.license_record
            print(f"- Seller: {seller.get('name', seller.get('proposerAccountId'))}")
            print(f"  License: {license_record.get('LicenseArn')}")
            print(f"  Product: {license_record.get('ProductName', 'Unknown')}")
            print(f"  Targets: {json.dumps(item.grant_targets)}")
            print(f"  Auto activate: {seller.get('autoActivateGrant', True)}")
            print(f"  Replace legacy grants: {seller.get('replaceLegacyGrants', False)}")
    else:
        print("No grants planned.")

    if plan.skipped:
        print()
        print("Skipped:")
        for item in plan.skipped:
            print(f"- {item}")

    if plan.blocked:
        print()
        print("Blocked:")
        for item in plan.blocked:
            print(f"- {item}")


def apply_plan(
    plan: BackfillPlan,
    region: str,
    organization_id: str,
    pending_grant_table_name: str | None = None,
) -> int:
    """Apply the planned grants, recording any that don't activate immediately.

    Grants that don't reach ACTIVE/WORKFLOW_COMPLETED on the first attempt are
    recorded in the pending-grants table, the same way handler.py's
    EventBridge-triggered path does. Without this, a backfilled grant that
    lands in DISABLED/PENDING_WORKFLOW has no automatic path to activation --
    the scheduled mppo-grant-activation-retry rule only retries grants it
    knows about, and this script previously never told it about any.
    """
    table_name = pending_grant_table_name or os.environ.get(
        "PENDING_GRANT_TABLE_NAME", DEFAULT_PENDING_GRANT_TABLE_NAME
    )
    failures = 0
    for item in plan.items:
        seller = item.seller
        license_record = item.license_record
        seller_name = seller.get("name", seller.get("proposerAccountId", "seller"))
        auto_activate = seller.get("autoActivateGrant", True)
        try:
            results = create_and_activate_grants(
                license_arn=license_record["LicenseArn"],
                grant_targets=item.grant_targets,
                seller_name=seller_name,
                product_name=license_record.get("ProductName", "Unknown"),
                organization_id=organization_id,
                home_region=region,
                auto_activate=auto_activate,
                replace_legacy_grants=seller.get("replaceLegacyGrants", False),
            )
            print(f"Applied {license_record['LicenseArn']}: {json.dumps(results, default=str)}")

            if auto_activate:
                for grant in results:
                    if grant.get("status") not in SUCCESS_STATUSES:
                        record_pending_grant(
                            table_name=table_name,
                            grant=grant,
                            agreement_id="backfill",
                            offer_id="backfill",
                            seller_name=seller_name,
                            product_name=license_record.get("ProductName", "Unknown"),
                            license_arn=license_record["LicenseArn"],
                            replace_legacy_grants=seller.get("replaceLegacyGrants", False),
                        )
                        print(
                            f"  Recorded pending activation for {grant.get('grant_arn')} "
                            f"(status: {grant.get('status')}) -- the scheduled retry rule "
                            "will continue until it activates."
                        )
        except Exception as error:
            failures += 1
            print(f"Failed {license_record.get('LicenseArn')}: {error}")

    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill License Manager grants for existing allowed Marketplace licenses."
    )
    parser.add_argument("--config", default="config/sellers.json", help="Path to sellers config.")
    parser.add_argument("--region", default="us-east-1", help="License Manager region.")
    parser.add_argument(
        "--license-arn",
        action="append",
        default=[],
        help="Explicit license ARN to backfill. Can be passed more than once.",
    )
    parser.add_argument(
        "--seller-account",
        action="append",
        default=[],
        help="Limit backfill to an allowed seller account ID. Can be passed more than once.",
    )
    parser.add_argument("--apply", action="store_true", help="Apply the planned grants.")
    parser.add_argument(
        "--pending-grant-table-name",
        default=None,
        help=(
            "DynamoDB table for tracking grants pending activation. Defaults to "
            "the PENDING_GRANT_TABLE_NAME env var, or 'mppo-pending-grants'. Grants "
            "that don't activate immediately are recorded here so the scheduled "
            "mppo-grant-activation-retry rule can retry them, same as grants "
            "created via the EventBridge-triggered handler path."
        ),
    )
    parser.add_argument(
        "--confirm-account-id",
        default=None,
        help="Required with --apply. Must match the current AWS account ID.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)

    lm = boto3.client("license-manager", region_name=args.region)
    licenses = list_received_licenses(lm)
    plan = build_backfill_plan(
        config=config,
        licenses=licenses,
        license_arns=args.license_arn,
        seller_accounts=args.seller_account,
    )

    print_plan(plan, args.apply)

    if plan.blocked:
        return 1

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply --confirm-account-id <account-id> to create grants.")
        return 0

    sts = boto3.client("sts", region_name=args.region)
    orgs = boto3.client("organizations", region_name=args.region)
    try:
        validation = validate_apply_context(sts, orgs, config, args.confirm_account_id)
    except ClientError as error:
        print(f"Apply validation failed: {error}")
        return 1
    if validation:
        return validation

    return apply_plan(plan, args.region, config["organizationId"], args.pending_grant_table_name)


if __name__ == "__main__":
    sys.exit(main())
