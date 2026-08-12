"""End-to-End Validation Script.

Walks through each component of the MPPO grants automation to verify
correct deployment and configuration. Does NOT require a live private offer —
it validates infrastructure, key API access, and simulates the Lambda event flow.

Usage:
    python scripts/e2e_validate.py --org-id o-xxxxxxxxxx --seller-account 444455556666

This script validates:
1. ✓ EventBridge rule exists and has correct pattern
2. ✓ Lambda function exists with correct config
3. ✓ DynamoDB table exists and is queryable
4. ✓ SNS topic exists
5. ✓ Key AWS APIs are reachable with the current credentials
6. ✓ License Manager is accessible
7. ✓ Organization prerequisites are configured
8. ✓ Lambda simulation with a synthetic Marketplace event

What it cannot validate (requires real Marketplace activity):
- License creation after subscription
- Grant distribution to member accounts
- Billing rate at negotiated price
"""

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

try:
    from bootstrap_prereqs import (
        check_delegated_admin,
        check_license_manager_settings,
        check_marketplace_trusted_access,
        check_service_linked_roles,
        get_caller_account_id,
        print_result,
    )
except ImportError:
    from scripts.bootstrap_prereqs import (
        check_delegated_admin,
        check_license_manager_settings,
        check_marketplace_trusted_access,
        check_service_linked_roles,
        get_caller_account_id,
        print_result,
    )


def check(label: str, passed: bool, detail: str = ""):
    """Print a check result."""
    icon = "✅" if passed else "❌"
    print(f"  {icon} {label}")
    if detail:
        print(f"     {detail}")
    return passed


def validate_eventbridge(region: str) -> bool:
    """Check EventBridge rule exists with correct pattern."""
    events = boto3.client("events", region_name=region)
    try:
        response = events.describe_rule(Name="mppo-agreement-created")
        pattern = json.loads(response.get("EventPattern", "{}"))

        source_ok = pattern.get("source") == ["aws.agreement-marketplace"]
        detail_type_ok = pattern.get("detail-type") == [
            "Purchase Agreement Created - Acceptor"
        ]
        enabled_ok = response.get("State") == "ENABLED"

        check("EventBridge rule exists", True, f"ARN: {response['Arn']}")
        check("Event source: aws.agreement-marketplace", source_ok)
        check("Detail-type: Purchase Agreement Created - Acceptor", detail_type_ok)
        check("Rule is ENABLED", enabled_ok)
        return source_ok and detail_type_ok and enabled_ok
    except ClientError as e:
        check("EventBridge rule exists", False, str(e))
        return False


def validate_lambda(region: str, org_id: str | None = None) -> bool:
    """Check Lambda function exists with correct configuration."""
    lam = boto3.client("lambda", region_name=region)
    try:
        response = lam.get_function(FunctionName="mppo-grants-handler")
        config = response["Configuration"]
        env_vars = config.get("Environment", {}).get("Variables", {})

        check("Lambda function exists", True, f"ARN: {config['FunctionArn']}")
        check("Runtime: python3.12", config["Runtime"] == "python3.12")
        check("HOME_REGION env var set", "HOME_REGION" in env_vars,
              f"HOME_REGION={env_vars.get('HOME_REGION', 'MISSING')}")
        check("ORGANIZATION_ID env var set", "ORGANIZATION_ID" in env_vars,
              f"ORGANIZATION_ID={env_vars.get('ORGANIZATION_ID', 'MISSING')}")
        org_matches = True
        if org_id:
            actual_org_id = env_vars.get("ORGANIZATION_ID")
            org_matches = actual_org_id == org_id
            check(
                "Lambda ORGANIZATION_ID matches expected org",
                org_matches,
                f"Expected: {org_id}, Lambda: {actual_org_id}",
            )
        check("SELLER_TABLE_NAME env var set", "SELLER_TABLE_NAME" in env_vars)
        check("SNS_TOPIC_ARN env var set", "SNS_TOPIC_ARN" in env_vars)
        return org_matches
    except ClientError as e:
        check("Lambda function exists", False, str(e))
        return False


def validate_dynamodb(region: str, seller_account: str | None = None) -> bool:
    """Check DynamoDB table exists and is queryable."""
    dynamodb = boto3.client("dynamodb", region_name=region)
    try:
        response = dynamodb.describe_table(TableName="mppo-allowed-sellers")
        status = response["Table"]["TableStatus"]
        check("DynamoDB table exists", True, f"Status: {status}")

        # Try to query if seller_account provided
        if seller_account:
            resource = boto3.resource("dynamodb", region_name=region)
            table = resource.Table("mppo-allowed-sellers")
            item_response = table.get_item(
                Key={"proposerAccountId": seller_account}
            )
            has_item = "Item" in item_response
            check(
                f"Seller {seller_account} in allow-list",
                has_item,
                f"Config: {json.dumps(item_response.get('Item', {}), default=str)}"
                if has_item else "Not found — run scripts/seed_sellers.py",
            )
            return has_item
        return True
    except ClientError as e:
        check("DynamoDB table exists", False, str(e))
        return False


def validate_sns(region: str) -> bool:
    """Check SNS topic exists."""
    sns = boto3.client("sns", region_name=region)
    try:
        topics = sns.list_topics()["Topics"]
        matching = [
            t for t in topics
            if "mppo-grants-notifications" in t["TopicArn"]
        ]
        if matching:
            check("SNS topic exists", True, f"ARN: {matching[0]['TopicArn']}")
            return True
        else:
            check("SNS topic exists", False, "Topic 'mppo-grants-notifications' not found")
            return False
    except ClientError as e:
        check("SNS topic exists", False, str(e))
        return False


def validate_license_manager(region: str) -> bool:
    """Check License Manager is accessible."""
    lm = boto3.client("license-manager", region_name=region)
    try:
        response = lm.list_received_licenses(MaxResults=1)
        license_count = len(response.get("Licenses", []))
        check(
            "License Manager accessible",
            True,
            f"Found {license_count} license(s) (showing max 1)",
        )
        return True
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "AccessDeniedException":
            check("License Manager accessible", False,
                  "AccessDenied — check IAM permissions and SLR setup")
        else:
            check("License Manager accessible", False, str(e))
        return False


def validate_organizations(region: str, org_id: str | None = None) -> bool:
    """Check Organizations access and all-features mode."""
    orgs = boto3.client("organizations", region_name=region)
    try:
        response = orgs.describe_organization()
        org = response["Organization"]
        actual_org_id = org["Id"]
        feature_set = org.get("FeatureSet", "UNKNOWN")

        check("Organizations accessible", True, f"Org ID: {actual_org_id}")
        all_features = feature_set == "ALL"
        check(
            "All features enabled (not just consolidated billing)",
            all_features,
            f"FeatureSet: {feature_set}"
            + (" ⚠️ Org-wide grants require ALL features!" if not all_features else ""),
        )

        if org_id and org_id != actual_org_id:
            check(
                f"Config org ID matches actual",
                False,
                f"Config: {org_id}, Actual: {actual_org_id}",
            )
            return False

        return all_features
    except ClientError as e:
        check("Organizations accessible", False, str(e))
        return False


def validate_prerequisites(region: str, delegated_admin_account_id: str | None = None) -> bool:
    """Validate License Manager and Marketplace org prerequisites."""
    lm = boto3.client("license-manager", region_name=region)
    orgs = boto3.client("organizations", region_name=region)
    iam = boto3.client("iam", region_name=region)
    sts = boto3.client("sts", region_name=region)

    results = [
        check_license_manager_settings(lm),
        check_marketplace_trusted_access(orgs),
        check_service_linked_roles(iam),
        check_delegated_admin(orgs, delegated_admin_account_id),
    ]

    for result in results:
        print("  ", end="")
        print_result(result)

    try:
        account_id = get_caller_account_id(sts)
        check("Caller account resolved", True, f"Account: {account_id}")
    except ClientError as e:
        check("Caller account resolved", False, str(e))
        return False

    return not any(result.blocker for result in results)


def simulate_event(region: str, seller_account: str) -> bool:
    """Invoke the deployed Lambda with a synthetic Marketplace event."""
    lambda_client = boto3.client("lambda", region_name=region)

    test_agreement_id = f"agmt-e2etest-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_detail = {
        "requestId": str(uuid.uuid4()),
        "catalog": "AWSMarketplace",
        "agreement": {
            "id": test_agreement_id,
            "intent": "NEW",
            "status": "ACTIVE",
            "acceptanceTime": now,
            "startTime": now,
            "endTime": "2026-01-01T00:00:00Z",
        },
        "acceptor": {"accountId": "111122223333"},
        "proposer": {"accountId": seller_account},
        "offer": {"id": "offer-e2etest123"},
    }
    event = {
        "source": "aws.agreement-marketplace",
        "detail-type": "Purchase Agreement Created - Acceptor",
        "detail": event_detail,
    }

    try:
        response = lambda_client.invoke(
            FunctionName="mppo-grants-handler",
            InvocationType="RequestResponse",
            Payload=json.dumps(event).encode(),
        )
        payload = json.loads(response["Payload"].read().decode() or "{}")
        function_error = response.get("FunctionError")

        if response.get("StatusCode") == 200 and not function_error:
            if payload.get("status") == "error" and payload.get("reason") == "license_not_found":
                detail = (
                    f"Agreement ID: {test_agreement_id}, Result: license_not_found "
                    "(expected for synthetic agreements without a new Marketplace license)"
                )
            else:
                detail = f"Agreement ID: {test_agreement_id}, Result: {json.dumps(payload)}"
            check(
                "Lambda test invocation successful",
                True,
                detail,
            )
            return True

        check(
            "Lambda test invocation successful",
            False,
            f"FunctionError={function_error}, Payload={json.dumps(payload)}",
        )
        return False
    except ClientError as e:
        check("Lambda test invocation successful", False, str(e))
        return False


def main():
    parser = argparse.ArgumentParser(
        description="End-to-end validation of MPPO grants automation"
    )
    parser.add_argument(
        "--org-id",
        default=None,
        help="Expected Organization ID (validates config matches)",
    )
    parser.add_argument(
        "--seller-account",
        default=None,
        help="Seller account ID to check in allow-list",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Invoke the deployed Lambda with a synthetic Marketplace event",
    )
    parser.add_argument(
        "--skip-orgs",
        action="store_true",
        help="Skip Organizations check (if running from non-mgmt account)",
    )
    parser.add_argument(
        "--delegated-admin-account-id",
        default=None,
        help="Optional License Manager delegated admin account ID to validate",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("MPPO Grants Automation — E2E Validation")
    print("=" * 60)
    print(f"Region: {args.region}")
    print()

    all_passed = True

    # 1. EventBridge
    print("📡 EventBridge Rule")
    if not validate_eventbridge(args.region):
        all_passed = False
    print()

    # 2. Lambda
    print("⚡ Lambda Function")
    if not validate_lambda(args.region, args.org_id):
        all_passed = False
    print()

    # 3. DynamoDB
    print("🗄️  DynamoDB Table")
    if not validate_dynamodb(args.region, args.seller_account):
        all_passed = False
    print()

    # 4. SNS
    print("📢 SNS Topic")
    if not validate_sns(args.region):
        all_passed = False
    print()

    # 5. License Manager
    print("📜 License Manager")
    if not validate_license_manager(args.region):
        all_passed = False
    print()

    # 6. Organizations
    if not args.skip_orgs:
        print("🏢 AWS Organizations")
        if not validate_organizations(args.region, args.org_id):
            all_passed = False
        print()

        print("🧰 Organization Prerequisites")
        if not validate_prerequisites(args.region, args.delegated_admin_account_id):
            all_passed = False
        print()

    # 7. Event simulation (optional)
    if args.simulate and args.seller_account:
        print("🧪 Event Simulation")
        if not simulate_event(args.region, args.seller_account):
            all_passed = False
        print()

    # Summary
    print("=" * 60)
    if all_passed:
        print("✅ All infrastructure checks PASSED")
        print()
        print("Next steps for full E2E validation:")
        print("  1. Accept a private offer in AWS Marketplace")
        print("  2. Wait 1-2 minutes for license creation")
        print("  3. Check Lambda logs for grant creation")
        print("  4. Run: python scripts/bedrock_discount_check.py --issuer \"AWS/Marketplace\"")
    else:
        print("❌ Some checks FAILED — review output above")
        print()
        print("Common fixes:")
        print("  • Deploy stack: cd cdk && cdk deploy")
        print("  • Seed sellers: python scripts/seed_sellers.py --config config/sellers.json")
        print("  • Enable all features: AWS Organizations → Settings")
        print("  • License Manager SLR: Open LM console in us-east-1")
    print("=" * 60)

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
