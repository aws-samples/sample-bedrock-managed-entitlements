"""Step 4: Send notifications via SNS."""

import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

sns = boto3.client("sns", region_name="us-east-1")


def notify_admins(
    topic_arn: str,
    subject: str,
    message: str,
) -> None:
    """Publish notification to SNS topic.

    Handles missing topic ARN gracefully (logs warning, doesn't fail).
    SNS subject is truncated to 100 chars per API limits.

    Args:
        topic_arn: SNS topic ARN
        subject: Notification subject line (max 100 chars)
        message: Notification body
    """
    if not topic_arn:
        logger.warning("No SNS topic ARN configured, skipping notification")
        return

    try:
        sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],  # SNS subject limit
            Message=message,
        )
        logger.info("Notification sent: %s", subject[:100])
    except ClientError as e:
        # Don't fail the workflow if notification fails
        logger.error("Failed to send notification: %s", str(e))
