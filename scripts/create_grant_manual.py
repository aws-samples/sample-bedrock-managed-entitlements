"""Manually create and activate a grant for an existing license.

Use this for licenses that were created before the automation was deployed.

Usage:
    python scripts/create_grant_manual.py \
        --license-arn arn:aws:license-manager::123456789012:license:lic-xxx \
        --org-id o-xxxxxxxxxx \
        --grant-name "Anthropic-Claude-org"
"""

import argparse
import json
import sys
import uuid

import boto3
from botocore.exceptions import ClientError


def main():
    parser = argparse.ArgumentParser(
        description="Manually create and activate a License Manager grant"
    )
    parser.add_argument(
        "--license-arn", required=True, help="License ARN to grant"
    )
    parser.add_argument(
        "--org-id", required=True, help="Organization ID (e.g., o-xxxxxxxxxx)"
    )
    parser.add_argument(
        "--grant-name", default=None, help="Grant name (auto-generated if not set)"
    )
    parser.add_argument(
        "--region", default="us-east-1", help="AWS region"
    )
    parser.add_argument(
        "--no-activate", action="store_true", help="Don't activate after creation"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would happen"
    )
    args = parser.parse_args()

    lm = boto3.client("license-manager", region_name=args.region)
    sts = boto3.client("sts")

    account_id = sts.get_caller_identity()["Account"]
    org_principal = f"arn:aws:organizations::{account_id}:organization/{args.org_id}"
    grant_name = args.grant_name or f"manual-grant-{args.org_id}"

    print(f"License ARN: {args.license_arn}")
    print(f"Organization: {args.org_id}")
    print(f"Principal ARN: {org_principal}")
    print(f"Grant name: {grant_name}")
    print()

    if args.dry_run:
        print("[DRY RUN] Would create grant with above parameters")
        return

    # Create grant
    print("Creating grant...")
    try:
        response = lm.create_grant(
            ClientToken=str(uuid.uuid4()),
            GrantName=grant_name,
            LicenseArn=args.license_arn,
            Principals=[org_principal],
            HomeRegion=args.region,
            AllowedOperations=[
                "CheckoutLicense",
                "CheckInLicense",
                "ExtendConsumptionLicense",
                "ListPurchasedLicenses",
                "CreateToken",
            ],
        )
        grant_arn = response["GrantArn"]
        print(f"  ✓ Grant created: {grant_arn}")
        print(f"  Status: {response.get('Status')}")
    except ClientError as e:
        print(f"  ✗ Failed: {e.response['Error']['Message']}")
        sys.exit(1)

    if args.no_activate:
        print("\nSkipping activation (--no-activate). Grant is in DISABLED state.")
        print("⚠️  Accounts will NOT get negotiated pricing until grant is activated.")
        return

    # Activate grant
    print("\nActivating grant...")
    import time

    for attempt in range(5):
        try:
            grant_info = lm.get_grant(GrantArn=grant_arn)
            status = grant_info["Grant"]["GrantStatus"]
            version = grant_info["Grant"]["Version"]
            print(f"  Current status: {status} (version: {version})")

            if status == "ACTIVE":
                print("  ✓ Already active!")
                break
            elif status == "DISABLED":
                response = lm.create_grant_version(
                    ClientToken=str(uuid.uuid4()),
                    GrantArn=grant_arn,
                    Status="ACTIVE",
                    SourceVersion=version,
                    Options={
                        "ActivationOverrideBehavior": "DISTRIBUTED_GRANTS_ONLY"
                    },
                )
                print(f"  ✓ Activated! New status: {response.get('Status')}")
                break
            else:
                print(f"  Waiting for grant to settle (attempt {attempt + 1}/5)...")
                time.sleep(5)
        except ClientError as e:
            print(f"  Attempt {attempt + 1} failed: {e.response['Error']['Message']}")
            time.sleep(5)
    else:
        print("  ⚠️ Could not activate within retry window. Check manually.")

    print("\nDone!")


if __name__ == "__main__":
    main()
