"""Tests for the SNS notification step."""

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))


@mock_aws
def test_notify_admins_success():
    """SNS notification is sent successfully."""
    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="test-topic")
    topic_arn = topic["TopicArn"]

    from steps.notify import notify_admins

    # Should not raise
    notify_admins(
        topic_arn=topic_arn,
        subject="Test Subject",
        message="Test message body with details",
    )


def test_notify_admins_empty_topic():
    """Empty topic ARN logs warning but doesn't raise."""
    from steps.notify import notify_admins

    # Should not raise
    notify_admins(
        topic_arn="",
        subject="Test Subject",
        message="Test message body",
    )


@mock_aws
def test_notify_admins_long_subject():
    """Subject longer than 100 chars is truncated."""
    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="test-topic")
    topic_arn = topic["TopicArn"]

    from steps.notify import notify_admins

    long_subject = "A" * 200
    # Should not raise — subject is truncated internally
    notify_admins(
        topic_arn=topic_arn,
        subject=long_subject,
        message="Body",
    )
