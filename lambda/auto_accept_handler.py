"""Auto-accept offers from trusted sellers (OPTIONAL — opt-in only).

⚠️  RISK WARNING: This Lambda automatically accepts private offers, which creates
    financial commitments. Only enable for sellers you fully trust with pre-negotiated
    terms. Review all implications before enabling.

This Lambda runs on a schedule (e.g., every hour) and:
1. Lists private purchase options (offers) visible to this account via the
   Marketplace Discovery API, filtered server-side to trusted sellers
2. Re-verifies each offer's seller against the DynamoDB allow-list
3. Auto-accepts matching offers using the Marketplace Agreement API
4. Confirms the resulting agreement's proposer account, and notifies admins

The existing grant automation then handles license distribution automatically.

Authorization:
An offer is identified by two fields on sellerOfRecord - sellerProfileId (an
AWS-assigned unique identifier) and displayName (a seller-chosen human-readable
string). Only sellerProfileId is an identity: display names are not unique, so
two sellers can present the same one. Authorization therefore keys exclusively
on sellerProfileId, which each allow-list record carries alongside the
proposerAccountId partition key. displayName is used for logging and
notifications only, and never gates acceptance.

The check is applied twice: as a SELLER_OF_RECORD_PROFILE_ID filter on
ListPurchaseOptions so untrusted offers are never returned, and again on each
offer after GetOffer so a filter regression cannot open the path. A seller
without sellerProfileId configured is skipped - the design fails closed.

After acceptance, DescribeAgreement reports the proposer's AWS account ID, which
is reconciled against the allow-list record's proposerAccountId. A mismatch
cannot prevent the agreement (it already exists by then), so it raises an alert.

API notes (verified against the marketplace-discovery 2026-02-05 and
marketplace-agreement 2020-03-01 service models - the Marketplace API does not
model an unaccepted offer as a "PROPOSAL" agreement; agreements only exist after
acceptance, so private offers must be discovered via marketplace-discovery, not
marketplace-agreement.SearchAgreements):
- ListPurchaseOptions(filters=[...]) lists private offers visible to this
  account. filterValues accepts at most 10 entries per filter, so trusted
  seller profile IDs are queried in chunks.
- GetOffer(offerId) returns agreementProposalId and sellerOfRecord
  ({sellerProfileId, displayName}). Note there is no AWS account ID here; the
  seller's account ID is only observable after acceptance, via DescribeAgreement.
- GetOfferTerms(offerId) returns the offer's terms; each term's "id" is
  required in CreateAgreementRequest's requestedTerms.
- CreateAgreementRequest(intent="NEW", agreementProposalIdentifier=...,
  requestedTerms=[{"id": ...}, ...]) returns an agreementRequestId.
- AcceptAgreementRequest(agreementRequestId=...) finalizes the agreement.
- DescribeAgreement(agreementId=...) returns proposer.accountId.

Prerequisites:
- Marketplace Discovery API access (GetOffer, GetOfferTerms, ListPurchaseOptions)
- Marketplace Agreement API access (CreateAgreementRequest, AcceptAgreementRequest,
  DescribeAgreement)
- Seller must be in the DynamoDB allow-list with autoAcceptOffers: true and a
  sellerProfileId

Event source: Scheduled (EventBridge Scheduler / CloudWatch Events cron)
"""

import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SELLER_TABLE_NAME = os.environ.get("SELLER_TABLE_NAME", "mppo-allowed-sellers")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
HOME_REGION = os.environ.get("HOME_REGION", "us-east-1")

MAX_FILTER_VALUES = 10


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

    auto_accept_sellers = _get_auto_accept_sellers(table)

    if not auto_accept_sellers:
        logger.info("No sellers have autoAcceptOffers enabled. Nothing to do.")
        return {"status": "no_auto_accept_sellers", "checked": 0, "accepted": 0}

    trusted_by_profile = {
        s["sellerProfileId"]: s
        for s in auto_accept_sellers
        if s.get("sellerProfileId")
    }
    unconfigured = [
        s.get("name", s["proposerAccountId"])
        for s in auto_accept_sellers
        if not s.get("sellerProfileId")
    ]

    if unconfigured:
        logger.warning(
            "Skipping %d seller(s) with autoAcceptOffers but no sellerProfileId: %s",
            len(unconfigured), ", ".join(unconfigured),
        )
        lines = ["The following sellers have autoAcceptOffers enabled but no sellerProfileId, so their offers cannot be verified and will NOT be auto-accepted:", ""]
        lines.extend(f"  - {name}" for name in unconfigured)
        lines.append("")
        lines.append("Add each seller's sellerProfileId (shown on the offer in the AWS Marketplace console) to config and re-run scripts/seed_sellers.py.")
        _notify(
            sns, SNS_TOPIC_ARN,
            subject=f"Auto-Accept Misconfigured ({len(unconfigured)} Seller(s))",
            message="\n".join(lines),
        )

    if not trusted_by_profile:
        logger.info("No auto-accept sellers have a sellerProfileId. Nothing to do.")
        return {
            "status": "no_verifiable_sellers",
            "checked": len(auto_accept_sellers),
            "accepted": 0,
            "unconfigured": len(unconfigured),
        }

    logger.info(
        "Checking private offers for %d auto-accept seller(s): %s",
        len(trusted_by_profile),
        ", ".join(s.get("name", s["proposerAccountId"]) for s in trusted_by_profile.values()),
    )

    accepted = []
    errors = []
    unauthorized = []

    try:
        private_offers = _list_private_offers(discovery_client, list(trusted_by_profile))
    except ClientError as e:
        error_msg = "Error listing private offers: " + e.response["Error"]["Message"]
        logger.error(error_msg)
        _notify(
            sns, SNS_TOPIC_ARN,
            subject="Auto-Accept Errors (1)",
            message="\n".join(["Errors during auto-accept:", "", "  - " + error_msg]),
        )
        return {"status": "completed", "checked": len(auto_accept_sellers), "accepted": 0, "errors": 1}

    for offer in private_offers:
        seller_name = offer.get("seller_name", "")
        profile_id = offer.get("seller_profile_id", "")

        seller = trusted_by_profile.get(profile_id) if profile_id else None
        if seller is None:
            logger.warning(
                "Skipping offer %s from unverified seller %s (profile %s)",
                offer.get("offer_id"), seller_name, profile_id or "unknown",
            )
            unauthorized.append(
                "offer " + str(offer.get("offer_id")) + ' presenting as "' + seller_name
                + '" (profile ' + (profile_id or "unknown") + ") is not in the allow-list"
            )
            continue

        try:
            result = _accept_offer(agreement_client, offer)
            agreement_id = result.get("agreementId")
            accepted.append({
                "seller": seller.get("name", seller_name),
                "offer_id": offer.get("offer_id"),
                "agreement_id": agreement_id,
            })
            logger.info(
                "Auto-accepted offer %s from %s -> agreement %s",
                offer.get("offer_id"), seller_name, agreement_id,
            )

            mismatch = _verify_agreement_proposer(
                agreement_client, agreement_id, seller.get("proposerAccountId"),
            )
            if mismatch:
                logger.error(mismatch)
                errors.append(mismatch)
        except ClientError as e:
            error_msg = (
                "Failed to accept offer " + str(offer.get("offer_id")) + " from "
                + seller_name + ": " + e.response["Error"]["Message"]
            )
            logger.error(error_msg)
            errors.append(error_msg)

    if accepted:
        lines = ["The following offers were automatically accepted:", ""]
        lines.extend(
            "  - " + a["seller"] + ": offer " + str(a["offer_id"]) + " -> agreement " + str(a["agreement_id"])
            for a in accepted
        )
        lines.append("")
        lines.append("Grant distribution will be handled automatically by the agreement-created event handler.")
        _notify(
            sns, SNS_TOPIC_ARN,
            subject=f"Auto-Accepted {len(accepted)} Offer(s)",
            message="\n".join(lines),
        )

    if unauthorized:
        lines = ["The following offers were NOT accepted because their seller profile is not in the allow-list:", ""]
        lines.extend("  - " + u for u in unauthorized)
        lines.append("")
        lines.append("An offer reaching this point means the seller filter did not apply as expected. Review these offers in the AWS Marketplace console before acting on them.")
        _notify(
            sns, SNS_TOPIC_ARN,
            subject=f"Auto-Accept Skipped {len(unauthorized)} Unverified Offer(s)",
            message="\n".join(lines),
        )

    if errors:
        lines = ["Errors during auto-accept:", ""]
        lines.extend("  - " + e for e in errors)
        _notify(
            sns, SNS_TOPIC_ARN,
            subject=f"Auto-Accept Errors ({len(errors)})",
            message="\n".join(lines),
        )

    return {
        "status": "completed",
        "checked": len(auto_accept_sellers),
        "accepted": len(accepted),
        "unauthorized": len(unauthorized),
        "unconfigured": len(unconfigured),
        "errors": len(errors),
    }


def _get_auto_accept_sellers(table) -> list:
    """Get all sellers with autoAcceptOffers=True from DynamoDB."""
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


def _list_private_offers(discovery_client, seller_profile_ids: list) -> list:
    """List private offers from the given sellers, with seller identity.

    Uses ListPurchaseOptions(VISIBILITY_SCOPE=PRIVATE,
    SELLER_OF_RECORD_PROFILE_ID=<trusted>) so offers from other sellers are
    never returned, then GetOffer for each to resolve the seller of record and
    the agreementProposalId needed to accept it.
    """
    offers = []
    paginator = discovery_client.get_paginator("list_purchase_options")

    for chunk_start in range(0, len(seller_profile_ids), MAX_FILTER_VALUES):
        chunk = seller_profile_ids[chunk_start:chunk_start + MAX_FILTER_VALUES]
        for page in paginator.paginate(
            filters=[
                {"filterType": "VISIBILITY_SCOPE", "filterValues": ["PRIVATE"]},
                {"filterType": "SELLER_OF_RECORD_PROFILE_ID", "filterValues": chunk},
            ],
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
                    "seller_profile_id": seller.get("sellerProfileId", ""),
                    "seller_name": seller.get("displayName", ""),
                })

    return offers


def _accept_offer(agreement_client, offer: dict) -> dict:
    """Accept a private offer.

    Callers must authorize the offer's seller before calling this.

    This is a three-step process:
    1. GetOfferTerms - fetch the offer's terms, which supplies the term IDs
       CreateAgreementRequest needs in requestedTerms
    2. CreateAgreementRequest - creates an agreement request from those terms
    3. AcceptAgreementRequest - finalizes the agreement

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


def _verify_agreement_proposer(
    agreement_client, agreement_id: str, expected_account_id: str,
) -> str:
    """Confirm an accepted agreement's proposer matches the allow-list record.

    sellerProfileId (pre-acceptance) and proposerAccountId (post-acceptance) are
    separate identity namespaces; this reconciles them. The agreement already
    exists by the time this runs, so a mismatch is reported, not prevented.

    Returns a description of the problem, or "" when the proposer checks out.
    """
    if not agreement_id or not expected_account_id:
        return ""

    try:
        agreement = agreement_client.describe_agreement(agreementId=agreement_id)
    except ClientError as e:
        return "Could not verify proposer of agreement " + agreement_id + ": " + e.response["Error"]["Message"]

    actual = (agreement.get("proposer", {}) or {}).get("accountId", "")
    if actual != expected_account_id:
        return (
            "Agreement " + agreement_id + " was accepted but its proposer account "
            + (actual or "unknown") + " does not match the allow-listed "
            + expected_account_id + ". Review this agreement immediately."
        )
    return ""


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
