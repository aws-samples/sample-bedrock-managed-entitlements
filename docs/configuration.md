# Configuration

How to prepare the organization, write `config/sellers.json`, and set up notifications. Both the CDK and Terraform deployments read this file.

## Organization prerequisites

Start with the [prerequisites in the README](../README.md#prerequisites), which run `scripts/bootstrap_prereqs.py --check`.

- **All features:** open the [AWS Organizations console](https://console.aws.amazon.com/organizations/) → Settings and confirm it says "All features" (not "Consolidated billing" only). The bootstrap script checks this setting but does not enable it, because AWS Organizations might need member account handshakes.
- If the check reports `APPLY` items, review them before making changes. Apply mode can enable organization-wide License Manager and Marketplace settings, so you must confirm the current AWS account ID:

```bash
python3 scripts/bootstrap_prereqs.py \
  --apply \
  --region us-east-1 \
  --confirm-account-id 123456789012
```

- This can enable License Manager organization integration and AWS Marketplace trusted access. It also checks the service-linked roles used by License Manager and Marketplace license management.
- If you use a License Manager delegated administrator account, pass and confirm it explicitly:

```bash
python3 scripts/bootstrap_prereqs.py \
  --apply \
  --region us-east-1 \
  --confirm-account-id 123456789012 \
  --delegated-admin-account-id 222233334444 \
  --confirm-delegated-admin-account-id 222233334444
```

## sellers.json

**Interactive setup (recommended):**

```bash
python3 scripts/setup_config.py
```

Auto-discovers your Organization ID, lists existing licenses, and generates config interactively.

**Manual config** (`config/sellers.json`):

```json
{
  "organizationId": "o-xxxxxxxxxx",
  "allowedSellers": [
    {
      "name": "Anthropic",
      "proposerAccountId": "123456789012",
      "issuerName": "AWS/Marketplace",
      "productSkus": ["prod-example"],
      "autoActivateGrant": true
    }
  ],
  "notifications": {
    "mode": "exceptions_only",
    "emailAddresses": ["admin@example.com"],
    "slackWorkspaceId": "",
    "slackChannelId": ""
  }
}
```

`productSkus` and `productNames` are optional for event-driven grants, but
required for safe scheduled reconciliation and backfill. An issuer name alone
can match unrelated products.

## Notifications

**Email** - add addresses to `notifications.emailAddresses`. Subscriptions are created automatically.

`notifications.mode` defaults to `exceptions_only`. This sends one scheduled
digest when offers remain unaccepted, grants are missing or inactive, or
configuration cannot be reconciled safely. It also sends execution-failure
alarms and a dead-man alarm if reconciliation stops completing. Set the mode
to `all` only if you also want a message for every successful acceptance and
activation.

AWS Marketplace also sends service-managed buyer notifications. Those emails
are outside this sample's SNS topic and cannot be suppressed here. Route them
to a dedicated mailbox or mail rule if they are too noisy; keep this sample's
exception digest going to an owned operational channel.

**Slack** (via [AWS Chatbot](https://docs.aws.amazon.com/chatbot/latest/adminguide/what-is.html)) - add your Slack workspace and channel IDs:

```json
"notifications": {
  "slackWorkspaceId": "T01ABCDEF",
  "slackChannelId": "C01ABCDEF"
}
```

To find these:
1. [Set up AWS Chatbot with Slack](https://docs.aws.amazon.com/chatbot/latest/adminguide/slack-setup.html) (one-time: authorize the AWS Chatbot app in your Slack workspace)
2. Workspace ID: visible in AWS Chatbot console after authorization
3. Channel ID: right-click the Slack channel → "Copy link" → the `C01...` segment is the channel ID

When configured, actionable reconciliation findings and errors are posted
directly to your Slack channel. Routine successes are posted only in `all`
mode.

**Finding the seller account ID:**

```bash
# From existing licenses
aws license-manager list-received-licenses --region us-east-1 \
    --query 'Licenses[].{Product:ProductName,Issuer:Issuer.Name}' --output table

# From existing agreements
aws marketplace-agreement search-agreements --catalog AWSMarketplace \
    --query 'AgreementViewSummaries[].{Id:AgreementId,Proposer:ProposerAccountId}' --output table

# Your Organization ID
aws organizations describe-organization --query 'Organization.Id' --output text
```
