# Deploy with AWS CDK

[`cdk/`](../cdk/) deploys the automation as one CloudFormation stack,
`MppoGrantsAutomationStack`. It contains the grant handler Lambda, the
seller allow-list and pending-grant DynamoDB tables, the SNS topic (with
optional email and Slack), the agreement-created, activation-retry, and
entitlement-monitor EventBridge rules, the Lambda error and monitor heartbeat
alarms, and the opt-in auto-accept Lambda. The Lambda code is the
[`lambda/`](../lambda/) directory as-is.

Use either CDK or Terraform for a given deployment, not both with the same
names.

## Prerequisites

- AWS CDK v2 and Python 3.12+, with the packages in
  [`cdk/requirements.txt`](../cdk/requirements.txt)
- The [prerequisites in the README](../README.md#prerequisites): run from the
  AWS Organizations management account, with License Manager and Marketplace
  organization integration in place
- Credentials for the management account. `cdk/app.py` fixes the region to
  `us-east-1`, where Marketplace agreement events and License Manager licenses
  live.

## Bootstrap

CDK needs its bootstrap resources once per account and region:

```bash
cd cdk && pip install -r requirements.txt
cdk bootstrap aws://ACCOUNT_ID/us-east-1
```

## Configuration

The stack reads `config/sellers.json` at synth time. If that file is missing,
it falls back to `config/sellers.example.json`, including its placeholder
organization ID, so create `config/sellers.json` first (see
[Configuration](configuration.md)).

| Setting | Default |
|---------|---------|
| `organizationId` | `o-xxxxxxxxxx` |
| `notifications.mode` | `exceptions_only` |
| `notifications.emailAddresses` | none (`admin@example.com` is skipped) |
| `notifications.slackWorkspaceId`, `slackChannelId` | none; Slack is set up only when both are set |
| `monitorSchedule` | `rate(1 day)` |
| `enableAutoAccept` | `false` |
| `autoAcceptSchedule` | `rate(1 hour)` |

Change the file and run `cdk deploy` again to apply new values. The seller
list itself lives in DynamoDB; re-run the seed script after changing it.

## Deploy

```bash
cd cdk
cdk diff     # review the changes
cdk deploy

# Seed the allow-list
cd .. && python3 scripts/seed_sellers.py --config config/sellers.json
```

The CDK stack always uses the default resource names, such as
`mppo-grants-handler` and `mppo-allowed-sellers`, so the scripts' defaults
match it.

## Outputs

| Output | Value |
|--------|-------|
| `LambdaFunctionArn` | MPPO handler Lambda ARN |
| `SellerTableName` | DynamoDB table for the seller allow-list |
| `PendingGrantTableName` | DynamoDB table for pending grant activation retries |
| `NotificationTopicArn` | SNS topic for notifications |
| `EventBridgeRuleName` | EventBridge rule name |
| `ActivationRetryRuleName` | EventBridge rule name for pending grant activation retries |
| `EntitlementMonitorRuleName` | EventBridge rule name for entitlement reconciliation |
| `AutoAcceptLambdaArn` | Auto-accept handler Lambda ARN (only when auto-accept is enabled) |
| `SlackChannelArn` | AWS Chatbot Slack channel configuration ARN (only when Slack is configured) |

## Differences from Terraform

See [Differences from the CDK stack](terraform.md#differences-from-the-cdk-stack).

## Checks

```bash
pip install -r requirements-dev.txt
pytest tests/test_cdk_stack.py
```

The assertion tests synthesize the stack and check its rules, tables,
Lambda settings, permissions, alarms, and the opt-in auto-accept resources.
They hide any local `config/sellers.json` while they run.

## Tear down

```bash
cd cdk && cdk destroy
```

The tables use `RemovalPolicy.DESTROY`, so `cdk destroy` deletes the
allow-list. Back it up first if you need it.
