"""Step 2: Discover the License Manager license created by subscription.

After a Marketplace subscription is accepted, a license is automatically created
in us-east-1. This step finds that license by filtering on the issuer (seller).

Note: Licenses may take 1-2 minutes to appear after subscription acceptance.
This function implements retry with exponential backoff.
"""

import logging
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def discover_license(
    proposer_account_id: str,
    issuer_name: str,
    acceptance_time: str,
    home_region: str = "us-east-1",
    max_retries: int = 5,
    base_delay: float = 2.0,
) -> Optional[dict]:
    """Find the License Manager license for a seller's subscription.

    Filters strictly on the seller's issuer name and requires the license to
    have been created at or after the agreement's acceptance time. Fails
    closed (returns None) on zero or ambiguous (multiple) matches rather than
    guessing — an unrelated pre-existing license must never be treated as the
    one issued by this agreement.

    Args:
        proposer_account_id: Seller's AWS account ID (used only for logging;
            License Manager does not expose the seller account on a license,
            so it cannot be used as a filter — see issuer_name instead)
        issuer_name: Expected `Issuer.Name` on the license, sourced from the
            seller allow-list entry. Required for a positive match.
        acceptance_time: ISO8601 agreement acceptance time from the
            EventBridge event. Licenses created before this are excluded.
        home_region: Region where licenses are created (always us-east-1)
        max_retries: Number of retry attempts
        base_delay: Base delay for exponential backoff (seconds)

    Returns:
        Dict with license_arn, product_name, product_sku, issuer_name
        or None if not found or the match is ambiguous
    """
    license_manager = boto3.client("license-manager", region_name=home_region)

    for attempt in range(max_retries):
        try:
            licenses = _list_all_licenses(license_manager)

            matching = []
            for lic in licenses:
                if lic.get("Status") != "AVAILABLE":
                    continue

                lic_issuer_name = lic.get("Issuer", {}).get("Name", "")
                create_time = lic.get("CreateTime", "")

                # Require an exact issuer match and a license created no
                # earlier than the agreement's acceptance — this is what
                # actually ties the license to this specific agreement,
                # since ProductSKU alone matches any license for the product.
                if lic_issuer_name != issuer_name:
                    continue
                if create_time and create_time < acceptance_time:
                    continue

                matching.append({
                    "license_arn": lic["LicenseArn"],
                    "product_name": lic.get("ProductName", ""),
                    "product_sku": lic.get("ProductSKU", ""),
                    "issuer_name": lic_issuer_name,
                    "create_time": create_time,
                })

            if len(matching) == 1:
                result = matching[0]
                logger.info(
                    "Found license: %s (product: %s, issuer: %s)",
                    result["license_arn"],
                    result["product_name"],
                    result["issuer_name"],
                )
                return result

            if len(matching) > 1:
                logger.error(
                    "Ambiguous license match for proposer %s (issuer %s): "
                    "%d licenses matched, expected exactly 1. Failing closed.",
                    proposer_account_id, issuer_name, len(matching),
                )
                return None

        except ClientError as e:
            logger.warning(
                "Attempt %d: Error listing licenses: %s", attempt + 1, str(e)
            )

        if attempt < max_retries - 1:
            wait_time = base_delay * (2 ** attempt)
            logger.info(
                "License not found yet (attempt %d/%d), waiting %.1fs...",
                attempt + 1, max_retries, wait_time,
            )
            time.sleep(wait_time)

    logger.error(
        "License not found after %d attempts for proposer %s (issuer %s)",
        max_retries, proposer_account_id, issuer_name,
    )
    return None


def _list_all_licenses(client) -> list:
    """List all received licenses, handling pagination."""
    licenses = []
    next_token = None

    while True:
        kwargs = {"MaxResults": 100}
        if next_token:
            kwargs["NextToken"] = next_token

        response = client.list_received_licenses(**kwargs)
        licenses.extend(response.get("Licenses", []))

        next_token = response.get("NextToken")
        if not next_token:
            break

    return licenses
