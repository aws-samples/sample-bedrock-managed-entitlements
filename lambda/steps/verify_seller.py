"""Step 1: Verify proposer (seller) against DynamoDB allow-list.

The DynamoDB table uses proposerAccountId as the partition key.
Each entry represents a seller whose MPPOs should be auto-processed.
"""

import logging
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb", region_name="us-east-1")


def verify_seller(
    proposer_account_id: str,
    table_name: str,
) -> Optional[dict]:
    """Check if the proposer (seller) account is in the allow-list.

    Args:
        proposer_account_id: AWS account ID of the seller/proposer
        table_name: DynamoDB table name

    Returns:
        Seller configuration dict if allowed, None otherwise.
        Expected fields: name, autoActivateGrant
    """
    if not proposer_account_id:
        logger.warning("Empty proposer account ID")
        return None

    table = dynamodb.Table(table_name)

    try:
        response = table.get_item(
            Key={"proposerAccountId": proposer_account_id}
        )
        if "Item" in response:
            logger.info(
                "Seller verified: %s (%s)",
                response["Item"].get("name", "Unknown"),
                proposer_account_id,
            )
            return response["Item"]
    except Exception as e:
        logger.error(
            "Error querying seller table for %s: %s",
            proposer_account_id, str(e),
        )
        raise

    logger.info("Proposer %s not found in allow-list", proposer_account_id)
    return None
