"""CDK Stack for MPPO Grants Automation.

Deploys:
- EventBridge rule to capture Marketplace agreement events (us-east-1)
- Lambda function to process events
- DynamoDB table for seller allow-list
- SNS topic for notifications
- IAM roles with least-privilege permissions
"""

import json
import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
)
from constructs import Construct


class MppoGrantsAutomationStack(Stack):
    """Stack for automated MPPO grant distribution.

    IMPORTANT: This stack must be deployed in us-east-1 because:
    - Marketplace agreement EventBridge events are sent to us-east-1
    - License Manager licenses are always created in us-east-1
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Load configuration
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "sellers.json"
        )
        if not os.path.exists(config_path):
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "config", "sellers.example.json"
            )
        with open(config_path) as f:
            config = json.load(f)

        organization_id = config.get("organizationId", "o-xxxxxxxxxx")

        # ─── DynamoDB: Seller Allow-List Table ───────────────────────────────
        seller_table = dynamodb.Table(
            self,
            "SellerAllowList",
            table_name="mppo-allowed-sellers",
            partition_key=dynamodb.Attribute(
                name="proposerAccountId", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery=True,
        )

        # ─── DynamoDB: Pending Grant Activation Retry Table ─────────────────
        pending_grant_table = dynamodb.Table(
            self,
            "PendingGrantActivations",
            table_name="mppo-pending-grants",
            partition_key=dynamodb.Attribute(
                name="grantArn", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery=True,
        )

        # ─── SNS: Notification Topic ────────────────────────────────────────
        notification_topic = sns.Topic(
            self,
            "MppoNotificationTopic",
            topic_name="mppo-grants-notifications",
            display_name="MPPO Grants Automation Notifications",
        )

        # Add email subscriptions from config
        for email in config.get("notifications", {}).get("emailAddresses", []):
            if email != "admin@example.com":  # Skip placeholder
                notification_topic.add_subscription(
                    subscriptions.EmailSubscription(email)
                )

        # ─── AWS Chatbot: Slack Integration (optional) ───────────────────────
        slack_workspace_id = config.get("notifications", {}).get("slackWorkspaceId", "")
        slack_channel_id = config.get("notifications", {}).get("slackChannelId", "")

        if slack_workspace_id and slack_channel_id:
            from aws_cdk import aws_chatbot as chatbot

            slack_channel = chatbot.SlackChannelConfiguration(
                self,
                "MppoSlackChannel",
                slack_channel_configuration_name="mppo-grants-notifications",
                slack_workspace_id=slack_workspace_id,
                slack_channel_id=slack_channel_id,
                notification_topics=[notification_topic],
            )

            cdk.CfnOutput(
                self, "SlackChannelArn",
                value=slack_channel.slack_channel_configuration_arn,
                description="AWS Chatbot Slack channel configuration ARN",
            )

        # ─── Lambda: MPPO Handler ────────────────────────────────────────────
        handler = lambda_.Function(
            self,
            "MppoHandler",
            function_name="mppo-grants-handler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "..", "lambda")
            ),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "SELLER_TABLE_NAME": seller_table.table_name,
                "PENDING_GRANT_TABLE_NAME": pending_grant_table.table_name,
                "SNS_TOPIC_ARN": notification_topic.topic_arn,
                "ORGANIZATION_ID": organization_id,
                "HOME_REGION": "us-east-1",
            },
            log_retention=logs.RetentionDays.TWO_WEEKS,
        )

        # Grant DynamoDB read access
        seller_table.grant_read_data(handler)
        pending_grant_table.grant_read_write_data(handler)

        # Grant SNS publish
        notification_topic.grant_publish(handler)

        # IAM: License Manager permissions
        handler.add_to_role_policy(
            iam.PolicyStatement(
                sid="LicenseManagerReadWrite",
                effect=iam.Effect.ALLOW,
                actions=[
                    "license-manager:ListReceivedLicenses",
                    "license-manager:CreateGrant",
                    "license-manager:CreateGrantVersion",
                    "license-manager:GetGrant",
                    "license-manager:ListDistributedGrants",
                ],
                resources=["*"],
            )
        )

        # IAM: Marketplace Agreement (read-only for enrichment)
        handler.add_to_role_policy(
            iam.PolicyStatement(
                sid="MarketplaceAgreementRead",
                effect=iam.Effect.ALLOW,
                actions=[
                    "aws-marketplace:DescribeAgreement",
                    "aws-marketplace:SearchAgreements",
                    "aws-marketplace:GetAgreementTerms",
                ],
                resources=["*"],
            )
        )

        # IAM: Organizations (validate org)
        handler.add_to_role_policy(
            iam.PolicyStatement(
                sid="OrganizationsRead",
                effect=iam.Effect.ALLOW,
                actions=[
                    "organizations:DescribeOrganization",
                ],
                resources=["*"],
            )
        )

        # IAM: STS (get account ID for principal ARN construction)
        handler.add_to_role_policy(
            iam.PolicyStatement(
                sid="STSGetCaller",
                effect=iam.Effect.ALLOW,
                actions=["sts:GetCallerIdentity"],
                resources=["*"],
            )
        )

        # ─── EventBridge: Marketplace Agreement Created Event ────────────────
        # Source: aws.agreement-marketplace (announced Nov 2025)
        # Detail-type: "Purchase Agreement Created - Acceptor"
        # Region: Events are delivered to us-east-1 default event bus
        rule = events.Rule(
            self,
            "MppoAgreementCreatedRule",
            rule_name="mppo-agreement-created",
            description="Captures Marketplace purchase agreement events for MPPO auto-grant distribution",
            event_pattern=events.EventPattern(
                source=["aws.agreement-marketplace"],
                detail_type=["Purchase Agreement Created - Acceptor"],
            ),
        )
        rule.add_target(targets.LambdaFunction(handler))

        # ─── EventBridge: Pending Grant Activation Retry ────────────────────
        activation_retry_rule = events.Rule(
            self,
            "MppoGrantActivationRetryRule",
            rule_name="mppo-grant-activation-retry",
            description="Retries License Manager grant activation for pending MPPO grants",
            schedule=events.Schedule.rate(Duration.hours(6)),
        )
        activation_retry_rule.add_target(
            targets.LambdaFunction(
                handler,
                event=events.RuleTargetInput.from_object({
                    "source": "mppo-grants-automation",
                    "detail-type": "Retry Pending MPPO Grant Activations",
                    "detail": {},
                }),
            )
        )

        # ─── Auto-Accept Lambda (OPTIONAL — disabled by default) ───────────
        # This Lambda checks for pending offers from trusted sellers and
        # auto-accepts them. Only deployed if enableAutoAccept is true.
        enable_auto_accept = config.get("enableAutoAccept", False)

        if enable_auto_accept:
            auto_accept_handler = lambda_.Function(
                self,
                "AutoAcceptHandler",
                function_name="mppo-auto-accept-handler",
                runtime=lambda_.Runtime.PYTHON_3_12,
                code=lambda_.Code.from_asset(
                    os.path.join(os.path.dirname(__file__), "..", "lambda")
                ),
                handler="auto_accept_handler.lambda_handler",
                timeout=Duration.minutes(5),
                memory_size=256,
                environment={
                    "SELLER_TABLE_NAME": seller_table.table_name,
                    "SNS_TOPIC_ARN": notification_topic.topic_arn,
                    "HOME_REGION": "us-east-1",
                },
                log_retention=logs.RetentionDays.TWO_WEEKS,
            )

            # Permissions
            seller_table.grant_read_data(auto_accept_handler)
            notification_topic.grant_publish(auto_accept_handler)

            auto_accept_handler.add_to_role_policy(
                iam.PolicyStatement(
                    sid="MarketplaceAgreementAccept",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "aws-marketplace:SearchAgreements",
                        "aws-marketplace:DescribeAgreement",
                        "aws-marketplace:GetAgreementTerms",
                        "aws-marketplace:CreateAgreementRequest",
                        "aws-marketplace:AcceptAgreementRequest",
                    ],
                    resources=["*"],
                )
            )

            auto_accept_handler.add_to_role_policy(
                iam.PolicyStatement(
                    sid="STSGetCaller",
                    effect=iam.Effect.ALLOW,
                    actions=["sts:GetCallerIdentity"],
                    resources=["*"],
                )
            )

            # Schedule: check every hour
            auto_accept_schedule = config.get("autoAcceptSchedule", "rate(1 hour)")
            auto_accept_rule = events.Rule(
                self,
                "AutoAcceptSchedule",
                rule_name="mppo-auto-accept-schedule",
                description="Periodically checks for pending offers to auto-accept (opt-in)",
                schedule=events.Schedule.expression(auto_accept_schedule),
            )
            auto_accept_rule.add_target(targets.LambdaFunction(auto_accept_handler))

            cdk.CfnOutput(
                self, "AutoAcceptLambdaArn",
                value=auto_accept_handler.function_arn,
                description="Auto-accept handler Lambda ARN (opt-in)",
            )

        # ─── Outputs ────────────────────────────────────────────────────────
        cdk.CfnOutput(
            self, "LambdaFunctionArn",
            value=handler.function_arn,
            description="MPPO handler Lambda ARN",
        )
        cdk.CfnOutput(
            self, "SellerTableName",
            value=seller_table.table_name,
            description="DynamoDB table for seller allow-list",
        )
        cdk.CfnOutput(
            self, "PendingGrantTableName",
            value=pending_grant_table.table_name,
            description="DynamoDB table for pending grant activation retries",
        )
        cdk.CfnOutput(
            self, "NotificationTopicArn",
            value=notification_topic.topic_arn,
            description="SNS topic for notifications",
        )
        cdk.CfnOutput(
            self, "EventBridgeRuleName",
            value=rule.rule_name,
            description="EventBridge rule name",
        )
        cdk.CfnOutput(
            self, "ActivationRetryRuleName",
            value=activation_retry_rule.rule_name,
            description="EventBridge rule name for pending grant activation retries",
        )
