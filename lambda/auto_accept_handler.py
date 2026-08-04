"""Auto-accept offers from trusted sellers (OPTIONAL — opt-in only).

⚠️  RISK WARNING: This Lambda automatically accepts private offers, which creates
    financial commitments. Only enable for sellers you fully trust with pre-negotiated
    terms. Review all implications before enabling.

This Lambda runs on a schedule (e.g., every hour) and:
1. Lists private purchase options (offers) visible to this account via the
   Marketplace Discovery API
2. Matches offers to trusted sellers via the DynamoDB allow-list
3. Auto-accepts matching offers using the Marketplace Agreement API
4. Notifies admins of any auto-accepted offers

The existing grant automation then handles license distribution automatically.

API notes (verified against the live Discovery/Agreement APIs — the
Marketplace API does not model an unaccepted offer as a "PROPOSAL" agreement;
agreements only exist after acceptance, so private offers must be discovered
via marketplace-discovery, not marketplace-agreement.SearchAgreements):
- ListPurchaseOptions(filters=[{"filterType": "VISIBILITY_SCOPE",
  "filterValues": ["PRIVATE"]}]) lists private offers visible to this account.
- GetOffer(offerId) returns agreementProposalId and sellerOfRecord, used to
  match the offer to an allow-listed seller and to build the agreement request.
- GetOfferTerms(offerId) returns the offer's terms; each term's "id" is
  required in CreateAgreementRequest's requestedTerms.
- CreateAgreementRequest(intent="NEW", agreementProposalIdentifier=...,
  requestedTerms=[{"id": ...}, ...]) returns an agreementRequestId.
- AcceptAgreementRequest(agreementRequestId=...) finalizes the agreement.

Prerequisites:
- Marketplace Discovery API access (GetOffer, GetOfferTerms, ListPurchaseOptions)
- Marketplace Agreement API access (CreateAgreementRequest, AcceptAgreementRequest)
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
    """Check for private offers from trusted sellers and auto-accept.

    This handler is triggered on a schedule (not by EventBridge events).
    It polls the Marketplace Discovery API for available private offers.
    """
    logger.info("Auto-accept check starting")

    dynamodb = boto3.resource("dynamodb", region_name=HOME_REGION)
    table = dynamodb.Table(SELLER_TABLE_NAME)
    discovery_client = boto3.client("marketplace-discovery", region_name=HOME_REGION)
    agreement_client = boto3.client("marketplace-agreement", region_name=HOME_REGION)
    sns = boto3.client("sns", region_name=HOME_REGION)

    # Get all sellers with autoAcceptOffers enabled
    auto_accept_sellers = _get_auto_accept_sellers(table)

    if not auto_accept_sellers:
        logger.info("No sellers have autoAcceptOffers enabled. Nothing to do.")
        return {"status": "no_auto_accept_sellers", "checked": 0, "accepted": 0}

    trusted_seller_names = {
        s.get("name", "").strip().lower()
        for s in auto_accept_sellers
        if s.get("name")
    }
    logger.info(
        "Checking private offers for %d auto-accept seller(s): %s",
        len(auto_accept_sellers),
        ", ".join(s.get("name", s["proposerAccountId"]) for s in auto_accept_sellers),
    )

    accepted = []
    errors = []

    try:
        private_offers = _list_private_offers(discovery_client)
    except ClientError as e:
        error_msg = f"Error listing private offers: {e.response['Error']['Message']}"
        logger.error(error_msg)
        _notify(
            sns, SNS_TOPIC_ARN,
            subject="Auto-Accept Errors (1)",
            message=f"Errors during auto-accept:\n\n  • {error_msg}",
        )
        return {"status": "completed", "checked": len(auto_accept_sellers), "accepted": 0, "errors": 1}

    for offer in private_offers:
        seller_name = offer.get("seller_name", "")
        if seller_name.strip().lower() not in trusted_seller_names:
            logger.info(
                "Skipping offer %s from untrusted/unmatched seller %s",
                offer.get("offer_id"), seller_name,
            )
            continue

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


def _list_private_offers(discovery_client) -> list:
    """List private offers currently visible to this account, with seller name.

    Uses ListPurchaseOptions(VISIBILITY_SCOPE=PRIVATE) to find candidate
    offers, then GetOffer for each to resolve the seller and the
    agreementProposalId needed to accept it.
    """
    offers = []
    paginator = discovery_client.get_paginator("list_purchase_options")
    for page in paginator.paginate(
        filters=[{"filterType": "VISIBILITY_SCOPE", "filterValues": ["PRIVATE"]}],
    ):
        for option in page.get("purchaseOptions", []):
            if option.get("purchaseOptionType") != "OFFER":
                continue
            offer_id = option.get("purchaseOptionId")
            try:
                offer_detail = discovery_client.get_offer(offerId=offer_id)
            except ClientError as e:
                logger.warning("GetOffer failed for %s: %s", offer_id, str(e))
                continue

            seller = offer_detail.get("sellerOfRecord", {}) or {}
            offers.append({
                "offer_id": offer_id,
                "agreement_proposal_id": offer_detail.get("agreementProposalId"),
                "seller_name": seller.get("name", ""),
            })

    return offers


def _accept_offer(agreement_client, offer: dict) -> dict:
    """Accept a private offer.

    This is a three-step process:
    1. GetOfferTerms — fetch the offer's terms, which supplies the term IDs
       CreateAgreementRequest needs in requestedTerms
    2. CreateAgreementRequest — creates an agreement request from those terms
    3. AcceptAgreementRequest — finalizes the agreement

    Note: For offers with configurable terms (e.g. ConfigurableUpfrontPricingTerm),
    additional configuration may be required per term. This sample assumes
    fixed/non-configurable pricing terms, matching typical MPPO offers.
    """
    offer_id = offer["offer_id"]
    agreement_proposal_id = offer["agreement_proposal_id"]
    if not agreement_proposal_id:
        raise ValueError(f"Offer {offer_id} has no agreementProposalId")

    discovery_client = boto3.client("marketplace-discovery", region_name=agreement_client.meta.region_name)
    requested_terms = []
    paginator = discovery_client.get_paginator("get_offer_terms")
    for page in paginator.paginate(offerId=offer_id):
        for term in page.get("offerTerms", []):
            for term_detail in term.values():
                if isinstance(term_detail, dict) and "id" in term_detail:
                    requested_terms.append({"id": term_detail["id"]})

    create_response = agreement_client.create_agreement_request(
        intent="NEW",
        agreementProposalIdentifier=agreement_proposal_id,
        requestedTerms=requested_terms,
    )
    agreement_request_id = create_response["agreementRequestId"]

    return agreement_client.accept_agreement_request(
        agreementRequestId=agreement_request_id,
    )


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
