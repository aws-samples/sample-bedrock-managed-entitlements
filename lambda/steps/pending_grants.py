"""Track and retry grants that are waiting for License Manager activation."""

import logging
from datetime import datetime, timezone

import boto3

from steps.create_grant import _activate_grant
from steps.notify import notify_admins

logger = logging.getLogger(__name__)

SUCCESS_STATUSES = {"ACTIVE", "WORKFLOW_COMPLETED"}


def _now_iso() -> str:
    """Return a UTC timestamp for DynamoDB records."""
    return datetime.now(timezone.utc).isoformat()


def record_pending_grant(
    table_name: str,
    grant: dict,
    agreement_id: str,
    offer_id: str,
    seller_name: str,
    product_name: str,
    license_arn: str,
    replace_legacy_grants: bool,
) -> None:
    """Record a grant that needs asynchronous activation retry."""
    if not table_name:
        logger.warning("No pending grant table configured; cannot record %s", grant)
        return

    grant_arn = grant.get("grant_arn")
    if not grant_arn:
        logger.warning("Grant result has no grant ARN; cannot record retry item")
        return

    table = boto3.resource("dynamodb", region_name="us-east-1").Table(table_name)
    now = _now_iso()
    table.put_item(
        Item={
            "grantArn": grant_arn,
            "licenseArn": license_arn,
            "agreementId": agreement_id,
            "offerId": offer_id,
            "sellerName": seller_name,
            "productName": product_name,
            "lastStatus": grant.get("status", "UNKNOWN"),
            "replaceLegacyGrants": replace_legacy_grants,
            "retryCount": 0,
            "createdAt": now,
            "updatedAt": now,
        }
    )
    logger.info("Recorded pending grant activation retry: %s", grant_arn)


def retry_pending_grant_activations(
    table_name: str,
    home_region: str,
    topic_arn: str,
) -> dict:
    """Retry activation for grants recorded as pending."""
    if not table_name:
        logger.warning("No pending grant table configured; skipping retry")
        return {"status": "skipped", "reason": "no_pending_table"}

    dynamodb = boto3.resource("dynamodb", region_name=home_region)
    table = dynamodb.Table(table_name)
    license_manager = boto3.client("license-manager", region_name=home_region)

    processed = 0
    completed = 0
    still_pending = 0
    failures = 0

    for item in _scan_all(table):
        processed += 1
        grant_arn = item["grantArn"]
        try:
            status = _activate_grant(
                license_manager,
                grant_arn,
                max_retries=3,
                poll_interval_seconds=0,
                replace_legacy=item.get("replaceLegacyGrants", False),
            )
            if status in SUCCESS_STATUSES:
                table.delete_item(Key={"grantArn": grant_arn})
                completed += 1
                notify_admins(
                    topic_arn=topic_arn,
                    subject=f"MPPO Grant Activated: {item.get('sellerName', 'Unknown')}",
                    message=(
                        f"Pending MPPO grant activation completed.\n\n"
                        f"Seller: {item.get('sellerName', 'Unknown')}\n"
                        f"Product: {item.get('productName', 'Unknown')}\n"
                        f"Agreement ID: {item.get('agreementId', 'N/A')}\n"
                        f"Offer ID: {item.get('offerId', 'N/A')}\n"
                        f"License ARN: {item.get('licenseArn', 'N/A')}\n"
                        f"Grant ARN: {grant_arn}\n"
                        f"Grant Status: {status}"
                    ),
                )
            else:
                _update_pending_item(table, grant_arn, status)
                still_pending += 1
        except Exception as e:
            failures += 1
            logger.exception("Failed to retry pending grant %s: %s", grant_arn, e)
            _update_pending_item(table, grant_arn, "RETRY_FAILED")

    return {
        "status": "success",
        "processed": processed,
        "completed": completed,
        "stillPending": still_pending,
        "failures": failures,
    }


def _scan_all(table) -> list[dict]:
    """Scan all pending grant records."""
    items = []
    kwargs = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def _update_pending_item(table, grant_arn: str, status: str) -> None:
    """Update retry metadata for a pending grant."""
    table.update_item(
        Key={"grantArn": grant_arn},
        UpdateExpression=(
            "SET lastStatus = :status, updatedAt = :updatedAt "
            "ADD retryCount :one"
        ),
        ExpressionAttributeValues={
            ":status": status,
            ":updatedAt": _now_iso(),
            ":one": 1,
        },
    )
