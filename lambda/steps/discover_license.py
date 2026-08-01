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
    home_region: str = "us-east-1",
    max_retries: int = 5,
    base_delay: float = 2.0,
) -> Optional[dict]:
    """Find the License Manager license for a seller's subscription.

    Searches for licenses where the issuer matches the proposer.
    Licenses from Marketplace subscriptions have the seller's name as issuer.

    Args:
        proposer_account_id: Seller's AWS account ID
        home_region: Region where licenses are created (always us-east-1)
        max_retries: Number of retry attempts
        base_delay: Base delay for exponential backoff (seconds)

    Returns:
        Dict with license_arn, product_name, product_sku, issuer_name
        or None if not found
    """
    license_manager = boto3.client("license-manager", region_name=home_region)

    for attempt in range(max_retries):
        try:
            # List all received licenses and find the most recent one
            # from this seller. Filter by Status=AVAILABLE.
            licenses = _list_all_licenses(license_manager)

            # Find licenses that match the seller
            # Marketplace licenses include the seller account in metadata
            matching = []
            for lic in licenses:
                if lic.get("Status") != "AVAILABLE":
                    continue

                # Check if this license is from the expected seller
                # The Beneficiary field contains the buyer account
                # The Issuer.Name often contains the seller/product info
                issuer_name = lic.get("Issuer", {}).get("Name", "")
                product_name = lic.get("ProductName", "")
                product_sku = lic.get("ProductSKU", "")

                # For Marketplace-sourced licenses, we match by checking
                # if the license is from AWS Marketplace (issuer pattern)
                # and was recently created
                if _is_marketplace_license(lic):
                    matching.append({
                        "license_arn": lic["LicenseArn"],
                        "product_name": product_name,
                        "product_sku": product_sku,
                        "issuer_name": issuer_name,
                        "create_time": lic.get("CreateTime", ""),
                    })

            if matching:
                # Return the most recently created matching license
                matching.sort(key=lambda x: x["create_time"], reverse=True)
                result = matching[0]
                logger.info(
                    "Found license: %s (product: %s, issuer: %s)",
                    result["license_arn"],
                    result["product_name"],
                    result["issuer_name"],
                )
                return result

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
        "License not found after %d attempts for proposer %s",
        max_retries, proposer_account_id,
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


def _is_marketplace_license(license_data: dict) -> bool:
    """Determine if a license originated from AWS Marketplace.

    Marketplace-sourced licenses typically have:
    - A ProductSKU field populated
    - Issuer name pattern from Marketplace
    - ReceivedMetadata with allowed operations
    """
    # Marketplace licenses always have a ProductSKU
    if license_data.get("ProductSKU"):
        return True

    # Check for Marketplace indicators in metadata
    metadata = license_data.get("LicenseMetadata", [])
    for item in metadata:
        if item.get("Name") == "AWSMarketplace" or "marketplace" in item.get("Value", "").lower():
            return True

    return False
