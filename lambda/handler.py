"""MPPO Grants Automation Lambda Handler.

Processes AWS Marketplace "Purchase Agreement Created - Acceptor" EventBridge events
and orchestrates:
1. Seller verification against DynamoDB allow-list
2. License discovery via License Manager
3. Organization-wide grant creation and activation
4. Admin notification via SNS

Event source: aws.agreement-marketplace
Detail-type: Purchase Agreement Created - Acceptor
Region: us-east-1 (all agreement events and licenses live here)
"""

import json
import logging
import os

from botocore.exceptions import ClientError

from steps.verify_seller import verify_seller
from steps.discover_license import discover_license
from steps.create_grant import create_and_activate_grant
from steps.notify import notify_admins
from steps.pending_grants import (
    SUCCESS_STATUSES,
    record_pending_grant,
    retry_pending_grant_activations,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
SELLER_TABLE_NAME = os.environ.get("SELLER_TABLE_NAME", "mppo-allowed-sellers")
PENDING_GRANT_TABLE_NAME = os.environ.get(
    "PENDING_GRANT_TABLE_NAME", "mppo-pending-grants"
)
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ORGANIZATION_ID = os.environ.get("ORGANIZATION_ID", "")
HOME_REGION = os.environ.get("HOME_REGION", "us-east-1")

RETRY_EVENT_SOURCE = "mppo-grants-automation"
RETRY_EVENT_DETAIL_TYPE = "Retry Pending MPPO Grant Activations"


def lambda_handler(event: dict, context) -> dict:
    """Main Lambda entry point.

    Expected event schema (EventBridge):
    {
        "source": "aws.agreement-marketplace",
        "detail-type": "Purchase Agreement Created - Acceptor",
        "detail": {
            "requestId": "...",
            "catalog": "AWSMarketplace",
            "agreement": {
                "id": "agmt-...",
                "intent": "NEW|RENEW|REPLACE",
                "status": "ACTIVE",
                "acceptanceTime": "2024-...",
                "startTime": "2024-...",
                "endTime": "2025-..."
            },
            "acceptor": { "accountId": "..." },
            "proposer": { "accountId": "..." },
            "offer": { "id": "offer-..." }
        }
    }
    """
    logger.info("Received event: %s", json.dumps(event))

    try:
        if (
            event.get("source") == RETRY_EVENT_SOURCE
            and event.get("detail-type") == RETRY_EVENT_DETAIL_TYPE
        ):
            logger.info("Processing scheduled pending grant activation retry")
            return retry_pending_grant_activations(
                table_name=PENDING_GRANT_TABLE_NAME,
                home_region=HOME_REGION,
                topic_arn=SNS_TOPIC_ARN,
            )

        # Parse the event
        detail = event.get("detail", {})
        agreement = detail.get("agreement", {})
        proposer = detail.get("proposer", {})
        acceptor = detail.get("acceptor", {})
        offer = detail.get("offer", {})

        agreement_id = agreement.get("id", "")
        proposer_account_id = proposer.get("accountId", "")
        offer_id = offer.get("id", "")
        intent = agreement.get("intent", "UNKNOWN")
        acceptance_time = agreement.get("acceptanceTime", "")

        if not agreement_id:
            logger.warning("No agreement ID in event, skipping")
            return {"status": "skipped", "reason": "no_agreement_id"}

        if not proposer_account_id:
            logger.warning("No proposer account ID in event, skipping")
            return {"status": "skipped", "reason": "no_proposer_account"}

        if not acceptance_time:
            logger.warning("No agreement acceptance time in event, skipping")
            return {"status": "skipped", "reason": "no_acceptance_time"}

        logger.info(
            "Processing agreement %s from proposer %s (intent: %s, offer: %s)",
            agreement_id, proposer_account_id, intent, offer_id,
        )

        # Step 1: Verify seller is in allow-list
        logger.info("Step 1: Verifying proposer %s", proposer_account_id)
        seller_config = verify_seller(
            proposer_account_id=proposer_account_id,
            table_name=SELLER_TABLE_NAME,
        )

        if not seller_config:
            logger.info(
                "Proposer %s not in allow-list, skipping", proposer_account_id
            )
            notify_admins(
                topic_arn=SNS_TOPIC_ARN,
                subject="MPPO Event - Seller Not in Allow-List",
                message=(
                    f"Received agreement event from proposer {proposer_account_id} "
                    f"but they are not in the allow-list. No action taken.\n\n"
                    f"Agreement ID: {agreement_id}\n"
                    f"Offer ID: {offer_id}\n"
                    f"Intent: {intent}\n\n"
                    f"To allow this seller, add their account ID to the "
                    f"DynamoDB table: {SELLER_TABLE_NAME}"
                ),
            )
            return {"status": "skipped", "reason": "seller_not_allowed"}

        seller_name = seller_config.get("name", proposer_account_id)
        issuer_name = seller_config["issuerName"]

        # Step 2: Discover the license created by this subscription
        logger.info("Step 2: Discovering license for agreement %s", agreement_id)
        license_info = discover_license(
            proposer_account_id=proposer_account_id,
            issuer_name=issuer_name,
            acceptance_time=acceptance_time,
            home_region=HOME_REGION,
        )

        if not license_info:
            msg = (
                f"Could not find License Manager license for agreement "
                f"{agreement_id} from {seller_name}. The license may take "
                f"1-2 minutes to appear after subscription. "
                f"If this persists, check License Manager in us-east-1."
            )
            logger.error(msg)
            notify_admins(
                topic_arn=SNS_TOPIC_ARN,
                subject=f"MPPO Grant FAILED - License Not Found ({seller_name})",
                message=msg,
            )
            return {"status": "error", "reason": "license_not_found"}

        license_arn = license_info["license_arn"]
        product_name = license_info.get("product_name", "Unknown")
        logger.info("Found license: %s (product: %s)", license_arn, product_name)

        # Step 3: Create and activate grants
        # Determine grant targets from seller config (default: entire organization)
        grant_targets = seller_config.get("grantTargets")
        if grant_targets:
            # Validate targets format. A non-empty grantTargets that reduces
            # to zero valid entries means the operator intended to scope this
            # seller and the config is broken — raise rather than silently
            # falling through to the org-wide default below.
            targets_list = []
            invalid = []
            for t in grant_targets:
                if isinstance(t, dict) and "type" in t and "id" in t:
                    targets_list.append(t)
                else:
                    invalid.append(t)
            if invalid:
                logger.warning("Invalid grant target(s) found: %s", invalid)
            if not targets_list:
                raise ValueError(
                    f"Seller {seller_name} has a non-empty grantTargets "
                    f"({grant_targets}) but none are valid — refusing to "
                    f"fall back to an org-wide grant. Fix the seller's "
                    f"grantTargets entries in the allow-list table."
                )
            logger.info(
                "Step 3: Creating %d grant(s) on license %s (targets: %s)",
                len(targets_list), license_arn,
                ", ".join(f"{t['type']}:{t['id']}" for t in targets_list),
            )
        else:
            # Default: entire organization
            targets_list = None
            logger.info(
                "Step 3: Creating org-wide grant for %s on license %s",
                ORGANIZATION_ID, license_arn,
            )

        auto_activate = seller_config.get("autoActivateGrant", True)
        replace_legacy = seller_config.get("replaceLegacyGrants", False)
        grant_result = create_and_activate_grant(
            license_arn=license_arn,
            organization_id=ORGANIZATION_ID,
            seller_name=seller_name,
            product_name=product_name,
            home_region=HOME_REGION,
            auto_activate=auto_activate,
            replace_legacy_grants=replace_legacy,
            grant_targets=targets_list,
        )
        logger.info("Grant result: %s", json.dumps(grant_result))

        # Step 4: Track pending activations and notify admins
        grant_count = grant_result.get("grant_count", 1)
        target_desc = (
            f"{grant_count} target(s)" if grant_targets
            else f"Organization {ORGANIZATION_ID}"
        )
        grant_results = grant_result.get("grants") or [grant_result]
        pending_grants = [
            grant for grant in grant_results
            if auto_activate and grant.get("status") not in SUCCESS_STATUSES
        ]
        for grant in pending_grants:
            record_pending_grant(
                table_name=PENDING_GRANT_TABLE_NAME,
                grant=grant,
                agreement_id=agreement_id,
                offer_id=offer_id,
                seller_name=seller_name,
                product_name=product_name,
                license_arn=license_arn,
                replace_legacy_grants=replace_legacy,
            )

        logger.info("Step 4: Sending notification")
        final_status = grant_result.get("status", "unknown")
        subject_status = "Pending Activation" if pending_grants else "Created"
        access_message = (
            f"Grant activation is pending. The scheduled retry job will "
            f"continue until License Manager reaches an activatable state."
            if pending_grants else
            f"Access distributed at the negotiated rate."
        )
        notify_admins(
            topic_arn=SNS_TOPIC_ARN,
            subject=f"MPPO Grant {subject_status}: {seller_name} - {product_name}",
            message=(
                f"Processed license distribution from {seller_name}.\n\n"
                f"Agreement ID: {agreement_id}\n"
                f"Offer ID: {offer_id}\n"
                f"Intent: {intent}\n"
                f"Product: {product_name}\n"
                f"License ARN: {license_arn}\n"
                f"Grant ARN: {grant_result.get('grant_arn', 'N/A')}\n"
                f"Grant Status: {final_status}\n"
                f"Target: {target_desc}\n\n"
                f"{access_message}"
            ),
        )

        return {
            "status": "success",
            "agreementId": agreement_id,
            "licenseArn": license_arn,
            "grantArn": grant_result.get("grant_arn"),
            "grantStatus": grant_result.get("status"),
        }

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_msg = e.response["Error"]["Message"]
        logger.error("AWS API error: %s - %s", error_code, error_msg)

        notify_admins(
            topic_arn=SNS_TOPIC_ARN,
            subject="MPPO Automation FAILED",
            message=(
                f"AWS API error processing MPPO event.\n\n"
                f"Error: {error_code} - {error_msg}\n\n"
                f"Event: {json.dumps(event, indent=2)}"
            ),
        )
        raise

    except Exception as e:
        logger.error("Unexpected error: %s", str(e), exc_info=True)
        notify_admins(
            topic_arn=SNS_TOPIC_ARN,
            subject="MPPO Automation FAILED - Unexpected Error",
            message=(
                f"Unexpected error: {str(e)}\n\n"
                f"Event: {json.dumps(event, indent=2)}"
            ),
        )
        raise
