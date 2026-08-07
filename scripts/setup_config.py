"""Interactive configuration setup.

Auto-discovers your Organization ID and available licenses, then generates
config/sellers.json interactively.

Run from the management account in us-east-1:
    python3 scripts/setup_config.py

Requires:
    - organizations:DescribeOrganization
    - license-manager:ListReceivedLicenses
    - sts:GetCallerIdentity
"""

import json
import os
import sys

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    sys.exit("boto3 is required. In CloudShell it's preinstalled; otherwise: pip install boto3")


REGION = "us-east-1"
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "sellers.json")


def get_account_id() -> str:
    """Get current AWS account ID."""
    sts = boto3.client("sts", region_name=REGION)
    return sts.get_caller_identity()["Account"]


def get_organization_id() -> str | None:
    """Auto-detect Organization ID."""
    orgs = boto3.client("organizations", region_name=REGION)
    try:
        response = orgs.describe_organization()
        org = response["Organization"]
        org_id = org["Id"]
        feature_set = org.get("FeatureSet", "UNKNOWN")
        mgmt_account = org.get("MasterAccountId", "unknown")
        return org_id, feature_set, mgmt_account
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "AWSOrganizationsNotInUseException":
            print("  ❌ AWS Organizations is not enabled for this account.")
            return None, None, None
        elif error_code == "AccessDeniedException":
            print("  ❌ Access denied. Run this from the management account.")
            return None, None, None
        raise


def list_licenses() -> list:
    """List all received licenses with issuer info."""
    lm = boto3.client("license-manager", region_name=REGION)
    licenses = []
    next_token = None

    while True:
        kwargs = {"MaxResults": 100}
        if next_token:
            kwargs["NextToken"] = next_token
        try:
            response = lm.list_received_licenses(**kwargs)
        except ClientError as e:
            if "Service role not found" in str(e):
                print("  ⚠️  License Manager not initialized. Open the License Manager")
                print("     console in us-east-1 to complete first-time setup.")
                return []
            raise
        licenses.extend(response.get("Licenses", []))
        next_token = response.get("NextToken")
        if not next_token:
            break

    return licenses


def extract_sellers(licenses: list) -> dict:
    """Extract unique sellers from licenses.

    Returns dict of {issuer_name: {account_ids, products}} based on available info.
    Note: License Manager doesn't directly expose the proposer account ID in all cases.
    We extract what we can from license metadata.
    """
    sellers = {}
    for lic in licenses:
        issuer = lic.get("Issuer", {})
        issuer_name = issuer.get("Name", "Unknown")
        product_name = lic.get("ProductName", "Unknown")
        product_sku = lic.get("ProductSKU", "")
        status = lic.get("Status", "UNKNOWN")
        license_arn = lic.get("LicenseArn", "")

        if issuer_name not in sellers:
            sellers[issuer_name] = {
                "products": [],
                "license_arns": [],
                "statuses": [],
            }

        sellers[issuer_name]["products"].append(product_name)
        sellers[issuer_name]["license_arns"].append(license_arn)
        sellers[issuer_name]["statuses"].append(status)

    return sellers


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt for yes/no."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        answer = input(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter y or n.")


def main():
    print("=" * 60)
    print("  Managed Entitlements for Amazon Bedrock — Configuration Setup")
    print("=" * 60)
    print()

    # Step 1: Identify account
    print("📋 Step 1: Identifying your account...")
    try:
        account_id = get_account_id()
        print(f"  Account ID: {account_id}")
    except Exception as e:
        print(f"  ❌ Cannot determine account: {e}")
        print("  Make sure AWS credentials are configured.")
        sys.exit(1)

    # Step 2: Detect Organization
    print()
    print("📋 Step 2: Detecting AWS Organization...")
    org_id, feature_set, mgmt_account = get_organization_id()

    if not org_id:
        print()
        print("  Cannot proceed without an Organization. Please:")
        print("  1. Enable AWS Organizations with 'all features'")
        print("  2. Run this script from the management account")
        sys.exit(1)

    print(f"  Organization ID: {org_id}")
    print(f"  Feature set: {feature_set}")
    print(f"  Management account: {mgmt_account}")

    if feature_set != "ALL":
        print()
        print(f"  ⚠️  Feature set is '{feature_set}', not 'ALL'.")
        print("  Org-wide grants require 'all features' mode.")
        print("  Enable it: AWS Organizations → Settings → Enable all features")
        if not prompt_yes_no("  Continue anyway?", default=False):
            sys.exit(1)

    if account_id != mgmt_account:
        print()
        print(f"  ⚠️  You're in account {account_id}, but the management account is {mgmt_account}.")
        print("  Grants must be created from the management account.")
        if not prompt_yes_no("  Continue anyway (for config generation)?", default=True):
            sys.exit(1)

    # Step 3: Discover licenses
    print()
    print("📋 Step 3: Discovering existing licenses in License Manager...")
    licenses = list_licenses()

    if not licenses:
        print("  No licenses found. This is normal if you haven't subscribed to any")
        print("  private offers yet. You can still configure sellers manually.")
        print()
        sellers = {}
    else:
        sellers = extract_sellers(licenses)
        print(f"  Found {len(licenses)} license(s) from {len(sellers)} seller(s):")
        print()
        for i, (issuer_name, info) in enumerate(sellers.items(), 1):
            products = list(set(info["products"]))
            statuses = list(set(info["statuses"]))
            print(f"  {i}. {issuer_name}")
            print(f"     Products: {', '.join(products)}")
            print(f"     Status: {', '.join(statuses)}")
            print()

    # Step 4: Build config
    print("📋 Step 4: Building configuration...")
    print()

    allowed_sellers = []

    if sellers:
        print("  Select which sellers to auto-accept (enter numbers separated by commas,")
        print("  or 'all' for all, or 'none' to add manually):")
        print()
        for i, issuer_name in enumerate(sellers.keys(), 1):
            print(f"    {i}. {issuer_name}")

        selection = input("\n  Selection [all]: ").strip().lower()

        if selection in ("", "all"):
            selected_names = list(sellers.keys())
        elif selection == "none":
            selected_names = []
        else:
            try:
                indices = [int(x.strip()) for x in selection.split(",")]
                seller_list = list(sellers.keys())
                selected_names = [seller_list[i - 1] for i in indices if 1 <= i <= len(seller_list)]
            except (ValueError, IndexError):
                print("  Invalid selection, using all.")
                selected_names = list(sellers.keys())

        for name in selected_names:
            print(f"\n  Configuring: {name}")
            proposer_id = input(f"    Seller AWS account ID (or press Enter to skip): ").strip()

            if not proposer_id:
                print("    ℹ️  To find the seller's account ID:")
                print("       - Check the private offer details in AWS Marketplace console")
                print("       - Or look at the EventBridge event: detail.proposer.accountId")
                print("       - Or ask the seller directly")
                proposer_id = input(f"    Seller AWS account ID: ").strip()

            if proposer_id:
                allowed_sellers.append({
                    "name": name,
                    "proposerAccountId": proposer_id,
                    "autoActivateGrant": True,
                    "replaceLegacyGrants": False,
                })
            else:
                print(f"    ⚠️  Skipping {name} (no account ID provided)")

    # Allow adding sellers manually
    if prompt_yes_no("\n  Add another seller manually?", default=not bool(allowed_sellers)):
        while True:
            name = input("    Seller name: ").strip()
            if not name:
                break
            proposer_id = input("    Seller AWS account ID: ").strip()
            if not proposer_id:
                print("    Skipped (no account ID)")
                continue
            allowed_sellers.append({
                "name": name,
                "proposerAccountId": proposer_id,
                "autoActivateGrant": True,
                "replaceLegacyGrants": False,
            })
            if not prompt_yes_no("    Add another?", default=False):
                break

    # Step 5: Email notifications
    print()
    print("📋 Step 5: Notification settings...")
    email = input("  Email for notifications (or Enter to skip): ").strip()
    emails = [email] if email else []

    # Generate config
    config = {
        "organizationId": org_id,
        "allowedSellers": allowed_sellers,
        "notifications": {
            "emailAddresses": emails,
        },
    }

    # Step 6: Write config
    print()
    print("📋 Step 6: Writing configuration...")
    print()
    print(json.dumps(config, indent=2))
    print()

    if prompt_yes_no(f"  Write to {CONFIG_PATH}?"):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        print(f"  ✅ Written to {CONFIG_PATH}")
    else:
        print("  Config not written. Copy the JSON above manually.")

    # Summary
    print()
    print("=" * 60)
    print("  Next Steps")
    print("=" * 60)
    print()
    print("  1. Review config/sellers.json")
    print("  2. Deploy: cd cdk && cdk deploy")
    print("  3. Seed DynamoDB: python scripts/seed_sellers.py --config config/sellers.json")
    print("  4. Validate: python scripts/e2e_validate.py --org-id " + org_id)
    print()
    if not allowed_sellers:
        print("  ⚠️  No sellers configured. Add them to config/sellers.json before deploying.")
    print()


if __name__ == "__main__":
    main()
