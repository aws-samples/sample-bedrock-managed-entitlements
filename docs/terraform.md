# Deploy with Terraform

[`terraform/`](../terraform/) deploys the same automation as the CDK stack: the
grant handler Lambda, the seller allow-list and pending-grant DynamoDB tables,
the SNS topic (with optional email and Slack), the agreement-created and
activation-retry EventBridge rules, and the opt-in auto-accept Lambda. It reads
the same `config/sellers.json`, ships the same `lambda/` code, and uses the same
resource names by default, so the helper scripts work unchanged.

Use either CDK or Terraform for a given deployment, not both with the same
names.

## Prerequisites

- Terraform 1.7 or later (the AWS provider is pinned to `>= 5.80, < 7.0`)
- The [prerequisites in the README](../README.md#prerequisites): run from the
  AWS Organizations management account, with License Manager and Marketplace
  organization integration in place
- Credentials from the standard AWS provider chain (`AWS_PROFILE`, SSO,
  environment variables). The region is fixed to `us-east-1`, where Marketplace
  agreement events and License Manager licenses live.

## Deploy

```bash
# Configure (same file the CDK stack uses)
python3 scripts/setup_config.py

cd terraform
terraform init
terraform plan -out=tfplan
terraform apply tfplan

# Seed the allow-list
cd .. && python3 scripts/seed_sellers.py --config config/sellers.json \
  --table-name "$(terraform -chdir=terraform output -raw seller_table_name)"
```

`terraform init` uses local state. For a shared or long-lived deployment,
add a [backend](https://developer.hashicorp.com/terraform/language/backend)
such as S3 before the first apply.

## Configuration

Values come from `config/sellers.json` unless a Terraform variable overrides
them. Without the file, set everything through variables. See
[`terraform.tfvars.example`](../terraform/terraform.tfvars.example).

| Variable | Default | Falls back to |
|----------|---------|---------------|
| `organization_id` | – (required) | `organizationId` |
| `name_prefix` | `mppo` | – |
| `notification_emails` | none | `notifications.emailAddresses` |
| `slack_workspace_id`, `slack_channel_id` | none | `notifications.slackWorkspaceId`, `slackChannelId` |
| `enable_auto_accept` | `false` | `enableAutoAccept` |
| `auto_accept_schedule` | `rate(1 hour)` | `autoAcceptSchedule` |
| `activation_retry_schedule` | `rate(6 hours)` | – |
| `monitor_schedule` | `rate(1 day)` | `monitorSchedule` |
| `notification_mode` | `exceptions_only` | `notifications.mode` |
| `agreement_rule_enabled` | `true` | – |
| `log_retention_days` | `14` | – |
| `sns_kms_master_key_id` | none | – |
| `config_path` | `../config/sellers.json` | – |
| `tags` | `{}` | – |

The plan fails before creating anything if the organization ID is missing or
only one of the two Slack IDs is set. The `admin@example.com` placeholder
from `sellers.example.json` is never subscribed.

## Running next to the CDK stack

To try Terraform in an account that already runs the CDK stack, choose a
different prefix so no names collide:

```bash
terraform apply -var name_prefix=mppo-tf
```

Both agreement rules then receive every Marketplace agreement event. Seed the
second allow-list with test sellers only, or set
`-var agreement_rule_enabled=false` so one agreement is not processed twice.
Pass the prefixed names to the scripts, for example
`scripts/simulate_event.py --live --function-name mppo-tf-grants-handler`.

## Differences from the CDK stack

- Log groups are Terraform resources with retention set up front, instead of
  the CDK log-retention custom resource, and `terraform destroy` removes them.
- Each Lambda role may write only to its own log group, rather than having
  `AWSLambdaBasicExecutionRole`. The other IAM statements match the CDK stack.
  DynamoDB stream actions are left out because the tables have no streams.
- DynamoDB tables use the AWS managed KMS key for encryption at rest.
- The Slack channel role has a `ReadOnlyAccess` guardrail, so the channel
  only receives notifications.
- `agreement_rule_enabled`, `activation_retry_schedule`, `monitor_schedule`,
  and `notification_mode` can be set.

## Checks

```bash
cd terraform
terraform fmt -check -recursive
terraform validate
terraform test        # offline, mocked AWS provider
checkov -d . --framework terraform
```

`terraform test` runs plan-only assertions that mirror
`tests/test_cdk_stack.py`. Each checkov skip is commented inline with its
reason, which in most cases is matching the CDK stack. The live run of this
module is recorded in [terraform-e2e-report.md](terraform-e2e-report.md).

## Tear down

```bash
cd terraform && terraform destroy
```

The tables have no deletion protection, matching the CDK stack's
`RemovalPolicy.DESTROY`. Back up the allow-list first if you need it.
