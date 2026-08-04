"""Seed the DynamoDB seller allow-list table from config/sellers.json.

Run after `cdk deploy` to populate the table:
    python scripts/seed_sellers.py --config config/sellers.json

The table uses proposerAccountId as the partition key.
"""

import argparse
import json

import boto3


def main():
    parser = argparse.ArgumentParser(
        description="Seed MPPO allowed sellers DynamoDB table"
    )
    parser.add_argument(
        "--config",
        default="config/sellers.json",
        help="Path to sellers config JSON",
    )
    parser.add_argument(
        "--table-name",
        default="mppo-allowed-sellers",
        help="DynamoDB table name",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print items without writing to DynamoDB",
    )
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    sellers = config.get("allowedSellers", [])
    if not sellers:
        print("No sellers found in config. Nothing to seed.")
        return

    if not args.dry_run:
        dynamodb = boto3.resource("dynamodb", region_name=args.region)
        table = dynamodb.Table(args.table_name)

    print(f"Seeding {len(sellers)} seller(s) into {args.table_name}...")
    print()

    for seller in sellers:
        item = {
            "proposerAccountId": seller["proposerAccountId"],
            "name": seller.get("name", "Unknown"),
            "issuerName": seller["issuerName"],
            "autoActivateGrant": seller.get("autoActivateGrant", True),
            "autoAcceptOffers": seller.get("autoAcceptOffers", False),
            "replaceLegacyGrants": seller.get("replaceLegacyGrants", False),
        }
        if "grantTargets" in seller:
            item["grantTargets"] = seller["grantTargets"]

        if args.dry_run:
            print(f"  [DRY RUN] Would write: {json.dumps(item)}")
        else:
            table.put_item(Item=item)
            print(f"  ✓ Added: {item['name']} ({item['proposerAccountId']})")

    print(f"\nDone! {'Would seed' if args.dry_run else 'Seeded'} {len(sellers)} seller(s).")


if __name__ == "__main__":
    main()
