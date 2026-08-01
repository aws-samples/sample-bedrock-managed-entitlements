"""Simulate an EventBridge event for local testing.

This script generates a realistic "Purchase Agreement Created - Acceptor" event
and either:
1. Invokes the Lambda handler locally (default)
2. Injects it into EventBridge via put-events (with --live flag)

Usage:
    # Local simulation (no AWS calls)
    python scripts/simulate_event.py --seller-account 444455556666 --agreement-id agmt-test123

    # Live injection into EventBridge (requires deployed stack)
    python scripts/simulate_event.py --seller-account 444455556666 --live
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone


def generate_event(
    seller_account_id: str,
    buyer_account_id: str = "111122223333",
    agreement_id: str | None = None,
    intent: str = "NEW",
) -> dict:
    """Generate a realistic Marketplace agreement EventBridge event."""
    if not agreement_id:
        agreement_id = f"agmt-{uuid.uuid4().hex[:24]}"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": "Purchase Agreement Created - Acceptor",
        "source": "aws.agreement-marketplace",
        "account": buyer_account_id,
        "time": now,
        "region": "us-east-1",
        "resources": [
            f"arn:aws:aws-marketplace::agreement:{agreement_id}"
        ],
        "detail": {
            "requestId": str(uuid.uuid4()),
            "catalog": "AWSMarketplace",
            "agreement": {
                "id": agreement_id,
                "intent": intent,
                "status": "ACTIVE",
                "acceptanceTime": now,
                "startTime": now,
                "endTime": "2026-12-31T23:59:59Z",
            },
            "acceptor": {
                "accountId": buyer_account_id,
            },
            "proposer": {
                "accountId": seller_account_id,
            },
            "offer": {
                "id": f"offer-{uuid.uuid4().hex[:12]}",
            },
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Simulate a Marketplace agreement EventBridge event"
    )
    parser.add_argument(
        "--seller-account",
        required=True,
        help="Seller/proposer AWS account ID",
    )
    parser.add_argument(
        "--buyer-account",
        default="111122223333",
        help="Buyer/acceptor AWS account ID",
    )
    parser.add_argument(
        "--agreement-id",
        default=None,
        help="Agreement ID (auto-generated if not provided)",
    )
    parser.add_argument(
        "--intent",
        choices=["NEW", "RENEW", "REPLACE"],
        default="NEW",
        help="Agreement intent type",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Inject into EventBridge (requires AWS credentials)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write event JSON to file instead of stdout",
    )
    args = parser.parse_args()

    event = generate_event(
        seller_account_id=args.seller_account,
        buyer_account_id=args.buyer_account,
        agreement_id=args.agreement_id,
        intent=args.intent,
    )

    if args.output:
        with open(args.output, "w") as f:
            json.dump(event, f, indent=2)
        print(f"Event written to {args.output}")
        return

    if args.live:
        import boto3

        client = boto3.client("events", region_name="us-east-1")
        response = client.put_events(
            Entries=[
                {
                    "Source": event["source"],
                    "DetailType": event["detail-type"],
                    "Detail": json.dumps(event["detail"]),
                    "Resources": event["resources"],
                }
            ]
        )
        print("EventBridge put_events response:")
        print(json.dumps(response, indent=2, default=str))

        failed = response.get("FailedEntryCount", 0)
        if failed > 0:
            print(f"\n⚠️  {failed} event(s) failed to inject")
            sys.exit(1)
        else:
            print("\n✅ Event injected successfully")
    else:
        # Local mode — invoke handler directly
        print("Generated event:")
        print(json.dumps(event, indent=2))
        print("\n--- Invoking handler locally ---\n")

        # Add lambda to path
        sys.path.insert(0, "lambda")
        from handler import lambda_handler

        result = lambda_handler(event, None)
        print("\nHandler result:")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
