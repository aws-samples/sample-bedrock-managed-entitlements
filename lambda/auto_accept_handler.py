"""Auto-accept offers from trusted sellers (OPTIONAL — opt-in only).

⚠️  RISK WARNING: This Lambda automatically accepts private offers, which creates
    financial commitments. Only enable for sellers you fully trust with pre-negotiated
    terms. Review all implications before enabling.

This Lambda runs on a schedule (e.g., every hour) and:
1. Searches for available/pending agreement proposals from trusted sellers
2. Auto-accepts offers that match the allow-list
3. Notifies admins of any auto-accepted offers

The existing grant automation then handles license distribution automatically.

Prerequisites:
- Marketplace Agreement API access (create_agreement_request, accept_agreement_request)
- Seller must be in the DynamoDB allow-list with autoAcceptOffers: true

Event source: Scheduled (EventBridge Scheduler / CloudWatch Events cron)
"""

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
SELLER_TABLE_NAME = os.environ.get("SELLER_TABLE_NAME", "mppo-allowed-sellers")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
HOME_REGION = os.environ.get("HOME_REGION", "us-east-1")


def lambda_handler(event: dict, context) -> dict:
    """Check for pending offers from trusted sellers and auto-accept.

    This handler is triggered on a schedule (not by EventBridge events).
    It polls the Marketplace Agreement API for available offers.
    """
    logger.info("Auto-accept check starting")

    dynamodb = boto3.resource("dynamodb", region_name=HOME_REGION)
    table = dynamodb.Table(SELLER_TABLE_NAME)
    agreement_client = boto3.client("marketplace-agreement", region_name=HOME_REGION)
    sns = boto3.client("sns", region_name=HOME_REGION)

    # Get all sellers with autoAcceptOffers enabled
    auto_accept_sellers = _get_auto_accept_sellers(table)

    if not auto_accept_sellers:
        logger.info("No sellers have autoAcceptOffers enabled. Nothing to do.")
        return {"status": "no_auto_accept_sellers", "checked": 0, "accepted": 0}

    logger.info(
        "Checking offers from %d auto-accept seller(s): %s",
        len(auto_accept_sellers),
        ", ".join(s.get("name", s["proposerAccountId"]) for s in auto_accept_sellers),
    )

    # Search for available agreements/proposals from these sellers
    accepted = []
    errors = []

    for seller in auto_accept_sellers:
        proposer_id = seller["proposerAccountId"]
        seller_name = seller.get("name", proposer_id)

        try:
            # Search for pending/available agreements from this seller
            pending_offers = _find_pending_offers(agreement_client, proposer_id)

            if not pending_offers:
                logger.info("No pending offers from %s", seller_name)
                continue

            logger.info(
                "Found %d pending offer(s) from %s",
                len(pending_offers), seller_name,
            )

            for offer in pending_offers:
                try:
                    result = _accept_offer(agreement_client, offer)
                    accepted.append({
                        "seller": seller_name,
                        "offer_id": offer.get("offer_id"),
                        "agreement_id": result.get("agreementId"),
                    })
                    logger.info(
                        "Auto-accepted offer %s from %s → agreement %s",
                        offer.get("offer_id"), seller_name,
                        result.get("agreementId"),
                    )
                except ClientError as e:
                    error_msg = f"Failed to accept offer {offer.get('offer_id')} from {seller_name}: {e.response['Error']['Message']}"
                    logger.error(error_msg)
                    errors.append(error_msg)

        except ClientError as e:
            error_msg = f"Error searching offers from {seller_name}: {e.response['Error']['Message']}"
            logger.error(error_msg)
            errors.append(error_msg)

    # Notify admins
    if accepted:
        _notify(
            sns, SNS_TOPIC_ARN,
            subject=f"Auto-Accepted {len(accepted)} Offer(s)",
            message=(
                f"The following offers were automatically accepted:\n\n"
                + "\n".join(
                    f"  • {a['seller']}: offer {a['offer_id']} → agreement {a['agreement_id']}"
                    for a in accepted
                )
                + "\n\nGrant distribution will be handled automatically by the "
                "agreement-created event handler."
            ),
        )

    if errors:
        _notify(
            sns, SNS_TOPIC_ARN,
            subject=f"Auto-Accept Errors ({len(errors)})",
            message="Errors during auto-accept:\n\n" + "\n".join(f"  • {e}" for e in errors),
        )

    return {
        "status": "completed",
        "checked": len(auto_accept_sellers),
        "accepted": len(accepted),
        "errors": len(errors),
    }


def _get_auto_accept_sellers(table) -> list:
    """Get all sellers with autoAcceptOffers=True from DynamoDB."""
    # Scan the table (small table, this is fine)
    try:
        response = table.scan()
        items = response.get("Items", [])
        return [
            item for item in items
            if item.get("autoAcceptOffers") is True
        ]
    except ClientError as e:
        logger.error("Error scanning seller table: %s", str(e))
        return []


def _find_pending_offers(client, proposer_account_id: str) -> list:
    """Find pending/available offers from a specific seller.

    Uses SearchAgreements to find proposals that haven't been accepted yet.
    """
    try:
        response = client.search_agreements(
            Catalog="AWSMarketplace",
            Filters=[
                {
                    "Name": "PartyType",
                    "Values": ["Acceptor"],
                },
                {
                    "Name": "Status",
                    "Values": ["PROPOSAL"],
                },
            ],
            MaxResults=50,
        )

        offers = []
        for agreement in response.get("AgreementViewSummaries", []):
            # Filter by proposer account
            proposer = agreement.get("ProposerAccountId", "")
            if proposer == proposer_account_id:
                offers.append({
                    "agreement_id": agreement.get("AgreementId"),
                    "offer_id": agreement.get("OfferId", ""),
                    "proposer_id": proposer,
                    "status": agreement.get("Status"),
                    "start_time": str(agreement.get("StartTime", "")),
                })

        return offers
    except ClientError as e:
        # SearchAgreements may not support PROPOSAL status filter in all cases
        logger.warning("SearchAgreements error: %s", str(e))
        return []


def _accept_offer(client, offer: dict) -> dict:
    """Accept a pending offer.

    This is a two-step process:
    1. CreateAgreementRequest — creates a quote/request
    2. AcceptAgreementRequest — finalizes the agreement

    Note: For simple private offers without configurable terms,
    the flow may be simplified. The exact API usage depends on the
    offer structure.
    """
    agreement_id = offer.get("agreement_id")

    # For offers that are in PROPOSAL state, we need to accept them
    # The accept_agreement_request API accepts the agreement request
    response = client.accept_agreement_request(
        agreementRequestId=agreement_id,
    )

    return response


def _notify(sns_client, topic_arn: str, subject: str, message: str) -> None:
    """Send SNS notification."""
    if not topic_arn:
        return
    try:
        sns_client.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],
            Message=message,
        )
    except ClientError as e:
        logger.error("Failed to send notification: %s", str(e))
