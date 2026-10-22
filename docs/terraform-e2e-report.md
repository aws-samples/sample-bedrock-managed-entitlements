# Terraform E2E report

A live run of [`terraform/`](../terraform/) against an AWS Organizations
management account in `us-east-1` that already runs the CDK stack, on
2026-10-22. Every name used the `mppo-tf` prefix so nothing touched the CDK
resources. All seller data was synthetic. Account and organization IDs are
redacted to `ACCOUNT_ID` and `o-xxxxxxxxxx`.

| Step | What it shows | Result |
|------|---------------|--------|
| [1. Static checks](#1-static-checks) | fmt, validate, `terraform test`, checkov | ✅ 9/9 tests, 0 checkov failures |
| [2. Plan and name check](#2-plan-and-name-check) | 21 resources, no name shared with the CDK stack | ✅ 0 collisions |
| [3. Apply](#3-apply) | Live apply | ✅ 21 added |
| [4. Seed sellers](#4-seed-sellers) | `scripts/seed_sellers.py --table-name` | ✅ |
| [5. Handler](#5-handler-with-synthetic-events) | Synthetic agreement events | ✅ allow-listed seller reaches license discovery; unknown seller skipped |
| [6. Retry rule](#6-activation-retry-rule) | Rule target payload, pending table | ✅ retry path runs, pending item updated |
| [7. Event pattern](#7-eventbridge-event-pattern) | Pattern matching, parity with CDK | ✅ identical to CDK rule |
| [8. Auto-accept](#8-auto-accept) | Opt-in Lambda and schedule | ✅ runs, accepts nothing |
| [9. Permissions and notifications](#9-schedules-permissions-and-notifications) | Logs, SNS metric | ✅ no AccessDenied, 2 publishes, retry schedule fired by itself |
| [10. Drift](#10-state-and-drift) | Re-plan after apply | ✅ no changes |
| [11. Destroy](#11-destroy) | `terraform destroy` | ✅ 21 destroyed |
| [12. Teardown check](#12-teardown-check) | Listing by prefix, CDK stack | ✅ nothing left, CDK stack unchanged |
| [13. Test suite](#13-python-test-suite) | `pytest tests/` | ✅ 93 passed |

**What the synthetic data does and does not cover.** The seller's issuer name
matches no real license, so the handler stops at license discovery and never
calls `CreateGrant`. The retry test uses a well-formed but nonexistent grant
ARN. The auto-accept seller profile ID matches no real seller, so no offer is
listed or accepted. Grant creation and activation against a real license are
covered by the unit tests and by the earlier live validation of the CDK stack,
which runs the same `lambda/` code. Slack (AWS Chatbot) was not enabled.

## Inputs

The run used this synthetic config, with `TF_VAR_name_prefix=mppo-tf`,
`TF_VAR_config_path` pointing at it, and `TF_VAR_organization_id` set to the
account's organization. Auto-accept was enabled with a daily schedule, and
the topic had no subscriptions.

```json
{
  "organizationId": "(overridden by TF_VAR_organization_id)",
  "enableAutoAccept": true,
  "autoAcceptSchedule": "rate(1 day)",
  "allowedSellers": [
    {
      "name": "Synthetic Seller (mppo-tf E2E)",
      "proposerAccountId": "444455556666",
      "sellerProfileId": "prof-mppotfsynthetic01",
      "issuerName": "Synthetic Issuer mppo-tf E2E",
      "autoActivateGrant": true,
      "autoAcceptOffers": true,
      "replaceLegacyGrants": false
    }
  ],
  "notifications": {
    "emailAddresses": [],
    "slackWorkspaceId": "",
    "slackChannelId": ""
  }
}
```

## 1. Static checks

```text
$ terraform version
Terraform v1.16.6
on linux_arm64
+ provider registry.terraform.io/hashicorp/archive v2.8.1
+ provider registry.terraform.io/hashicorp/aws v6.69.1

$ terraform fmt -check -recursive
exit=0

$ terraform validate
Success! The configuration is valid.


$ terraform test   # offline, mocked AWS provider
tests/stack.tftest.hcl... in progress
  run "defaults_match_cdk_names"... pass
  run "handler_configuration"... pass
  run "eventbridge_rules"... pass
  run "auto_accept_disabled_by_default"... pass
  run "auto_accept_enabled"... pass
  run "name_prefix_applies_everywhere"... pass
  run "placeholder_email_is_skipped"... pass
  run "missing_organization_id_fails"... pass
  run "slack_ids_must_be_set_together"... pass
tests/stack.tftest.hcl... tearing down
tests/stack.tftest.hcl... pass

Success! 9 passed, 0 failed.

$ checkov -d terraform --framework terraform --quiet --compact
terraform scan results:

Passed checks: 81, Failed checks: 0, Skipped checks: 20


# Skipped checks (each skip is an inline #checkov:skip with its justification)
      1 #checkov:skip=CKV_AWS_111:CreateGrant/CreateGrantVersion/AcceptGrant must act on licenses issued after deployment; matches the CDK stack.
      1 #checkov:skip=CKV_AWS_115:Invoked only by its schedule; no reserved concurrency, matching CDK.
      1 #checkov:skip=CKV_AWS_115:Invoked only by low-volume EventBridge rules; no reserved concurrency, matching CDK.
      2 #checkov:skip=CKV_AWS_116:Matches the CDK stack; failures are reported through the SNS topic.
      2 #checkov:skip=CKV_AWS_117:Calls only public AWS APIs; no VPC resources to reach, matching CDK.
      2 #checkov:skip=CKV_AWS_119:Encrypted with the AWS managed DynamoDB key; the table holds seller IDs and grant ARNs, not secrets.
      2 #checkov:skip=CKV_AWS_158:Matches the CDK stack, which uses the default CloudWatch Logs encryption.
      1 #checkov:skip=CKV_AWS_173:Environment variables hold resource names and the organization ID, not secrets.
      1 #checkov:skip=CKV_AWS_173:Environment variables hold resource names, not secrets.
      2 #checkov:skip=CKV_AWS_272:Code signing is not configured in the CDK stack this mirrors.
      2 #checkov:skip=CKV_AWS_338:Retention mirrors the CDK stack (14 days) and is configurable via log_retention_days.
      1 #checkov:skip=CKV_AWS_356:Grant and license ARNs are created by AWS Marketplace and unknown in advance; matches the CDK stack.
      2 #checkov:skip=CKV_AWS_50:X-Ray tracing is not enabled in the CDK stack this mirrors.
```

## 2. Plan and name check

The plan, then every planned name compared with the `mppo*` names that already existed in the account before the apply.

```text
$ TF_VAR_name_prefix=mppo-tf TF_VAR_config_path=<synthetic sellers.json> TF_VAR_organization_id=o-xxxxxxxxxx terraform plan -out=e2e.tfplan
  # data.aws_iam_policy_document.auto_accept[0] will be read during apply
  # (config refers to values not yet known)
  # data.aws_iam_policy_document.handler will be read during apply
  # (config refers to values not yet known)
  # aws_cloudwatch_event_rule.activation_retry will be created
  # aws_cloudwatch_event_rule.agreement_created will be created
  # aws_cloudwatch_event_rule.auto_accept[0] will be created
  # aws_cloudwatch_event_target.activation_retry will be created
  # aws_cloudwatch_event_target.agreement_created will be created
  # aws_cloudwatch_event_target.auto_accept[0] will be created
  # aws_cloudwatch_log_group.auto_accept[0] will be created
  # aws_cloudwatch_log_group.handler will be created
  # aws_dynamodb_table.allowed_sellers will be created
  # aws_dynamodb_table.pending_grants will be created
  # aws_iam_role.auto_accept[0] will be created
  # aws_iam_role.handler will be created
  # aws_iam_role_policy.auto_accept[0] will be created
  # aws_iam_role_policy.handler will be created
  # aws_lambda_function.auto_accept[0] will be created
  # aws_lambda_function.handler will be created
  # aws_lambda_permission.activation_retry will be created
  # aws_lambda_permission.agreement_created will be created
  # aws_lambda_permission.auto_accept[0] will be created
  # aws_sns_topic.notifications will be created
  # terraform_data.config_checks will be created
Plan: 21 to add, 0 to change, 0 to destroy.
```
```text
# Names in the plan (name_prefix=mppo-tf)
/aws/lambda/mppo-tf-auto-accept-handler
/aws/lambda/mppo-tf-grants-handler
mppo-tf-agreement-created
mppo-tf-allowed-sellers
mppo-tf-auto-accept-handler
mppo-tf-auto-accept-schedule
mppo-tf-grant-activation-retry
mppo-tf-grants-handler
mppo-tf-grants-notifications
mppo-tf-pending-grants

# Existing mppo* names in the account before apply (the CDK stack)
/aws/lambda/mppo-grants-handler
MppoGrantsAutomationStack-LogRetentionaae0aa3c5b4d4-adYxbh3skiBR
MppoGrantsAutomationStack-MppoHandlerServiceRole059-Xrdmkv81xNfb
mppo-agreement-created
mppo-allowed-sellers
mppo-grant-activation-retry
mppo-grants-handler
mppo-grants-notifications
mppo-pending-grants

$ comm -12 planned existing   # names in both lists (expect none)
collisions: 0
```

## 3. Apply

`terraform apply` of the saved plan.

```text
$ terraform apply e2e.tfplan
terraform_data.config_checks: Creating...
terraform_data.config_checks: Creation complete after 0s [id=c2dec23f-9980-abcd-af35-4149b3a45696]
aws_cloudwatch_event_rule.agreement_created: Creating...
aws_cloudwatch_event_rule.activation_retry: Creating...
aws_cloudwatch_log_group.auto_accept[0]: Creating...
aws_cloudwatch_event_rule.auto_accept[0]: Creating...
aws_cloudwatch_log_group.handler: Creating...
aws_iam_role.auto_accept[0]: Creating...
aws_sns_topic.notifications: Creating...
aws_iam_role.handler: Creating...
aws_dynamodb_table.allowed_sellers: Creating...
aws_dynamodb_table.pending_grants: Creating...
aws_cloudwatch_log_group.handler: Creation complete after 0s [id=/aws/lambda/mppo-tf-grants-handler]
aws_cloudwatch_log_group.auto_accept[0]: Creation complete after 0s [id=/aws/lambda/mppo-tf-auto-accept-handler]
aws_cloudwatch_event_rule.activation_retry: Creation complete after 0s [id=mppo-tf-grant-activation-retry]
aws_cloudwatch_event_rule.agreement_created: Creation complete after 0s [id=mppo-tf-agreement-created]
aws_cloudwatch_event_rule.auto_accept[0]: Creation complete after 0s [id=mppo-tf-auto-accept-schedule]
aws_iam_role.handler: Creation complete after 0s [id=mppo-tf-grants-handler]
aws_iam_role.auto_accept[0]: Creation complete after 0s [id=mppo-tf-auto-accept-handler]
aws_sns_topic.notifications: Creation complete after 0s [id=arn:aws:sns:us-east-1:ACCOUNT_ID:mppo-tf-grants-notifications]
aws_dynamodb_table.allowed_sellers: Still creating... [00m10s elapsed]
aws_dynamodb_table.pending_grants: Still creating... [00m10s elapsed]
aws_dynamodb_table.pending_grants: Creation complete after 10s [id=mppo-tf-pending-grants]
aws_dynamodb_table.allowed_sellers: Creation complete after 16s [id=mppo-tf-allowed-sellers]
data.aws_iam_policy_document.auto_accept[0]: Reading...
data.aws_iam_policy_document.handler: Reading...
data.aws_iam_policy_document.auto_accept[0]: Read complete after 0s [id=4279628829]
data.aws_iam_policy_document.handler: Read complete after 0s [id=3277912114]
aws_iam_role_policy.auto_accept[0]: Creating...
aws_iam_role_policy.handler: Creating...
aws_iam_role_policy.auto_accept[0]: Creation complete after 0s [id=mppo-tf-auto-accept-handler:mppo-tf-auto-accept-handler]
aws_lambda_function.auto_accept[0]: Creating...
aws_iam_role_policy.handler: Creation complete after 0s [id=mppo-tf-grants-handler:mppo-tf-grants-handler]
aws_lambda_function.handler: Creating...
aws_lambda_function.auto_accept[0]: Creation complete after 6s [id=mppo-tf-auto-accept-handler]
aws_lambda_permission.auto_accept[0]: Creating...
aws_cloudwatch_event_target.auto_accept[0]: Creating...
aws_cloudwatch_event_target.auto_accept[0]: Creation complete after 0s [id=mppo-tf-auto-accept-schedule-terraform-8d8H7ZGbegKthLYttAWeLxOU74]
aws_lambda_permission.auto_accept[0]: Creation complete after 0s [id=AllowAutoAcceptSchedule]
aws_lambda_function.handler: Still creating... [00m10s elapsed]
aws_lambda_function.handler: Creation complete after 12s [id=mppo-tf-grants-handler]
aws_lambda_permission.activation_retry: Creating...
aws_lambda_permission.agreement_created: Creating...
aws_cloudwatch_event_target.activation_retry: Creating...
aws_cloudwatch_event_target.agreement_created: Creating...
aws_cloudwatch_event_target.activation_retry: Creation complete after 0s [id=mppo-tf-grant-activation-retry-terraform-tjHXtfgtrw0dIQFA84p8YVqgaW]
aws_lambda_permission.activation_retry: Creation complete after 0s [id=AllowActivationRetryRule]
aws_cloudwatch_event_target.agreement_created: Creation complete after 0s [id=mppo-tf-agreement-created-terraform-36gcPdXHBcpZhUZOzJsQsGrU1k]
aws_lambda_permission.agreement_created: Creation complete after 0s [id=AllowAgreementCreatedRule]

Apply complete! Resources: 21 added, 0 changed, 0 destroyed.

Outputs:

activation_retry_rule_name = "mppo-tf-grant-activation-retry"
auto_accept_lambda_arn = "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:mppo-tf-auto-accept-handler"
event_bridge_rule_name = "mppo-tf-agreement-created"
lambda_function_arn = "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:mppo-tf-grants-handler"
lambda_function_name = "mppo-tf-grants-handler"
notification_topic_arn = "arn:aws:sns:us-east-1:ACCOUNT_ID:mppo-tf-grants-notifications"
pending_grant_table_name = "mppo-tf-pending-grants"
seller_table_name = "mppo-tf-allowed-sellers"
exit=0
```

## 4. Seed sellers

```text
$ python3 scripts/seed_sellers.py --config <synthetic sellers.json> --table-name "$(terraform -chdir=terraform output -raw seller_table_name)"
Seeding 1 seller(s) into mppo-tf-allowed-sellers...

  ✓ Added: Synthetic Seller (mppo-tf E2E) (444455556666)

Done! Seeded 1 seller(s).

$ aws dynamodb scan --table-name mppo-tf-allowed-sellers
{
    "Items": [
        {
            "issuerName": {
                "S": "Synthetic Issuer mppo-tf E2E"
            },
            "autoAcceptOffers": {
                "BOOL": true
            },
            "autoActivateGrant": {
                "BOOL": true
            },
            "proposerAccountId": {
                "S": "444455556666"
            },
            "sellerProfileId": {
                "S": "prof-mppotfsynthetic01"
            },
            "replaceLegacyGrants": {
                "BOOL": false
            },
            "name": {
                "S": "Synthetic Seller (mppo-tf E2E)"
            }
        }
    ],
    "Count": 1,
    "ScannedCount": 1,
    "ConsumedCapacity": null
}
```

## 5. Handler with synthetic events

`scripts/simulate_event.py --live` sends an EventBridge-shaped agreement event. The allow-listed synthetic seller passes verification and reaches license discovery. That step returns `license_not_found` after its retries because no real license has the synthetic issuer name. An unknown seller is skipped. Both paths publish to the SNS topic.

```text
$ python3 scripts/simulate_event.py --seller-account 444455556666 --live --function-name mppo-tf-grants-handler
Lambda invoke response:
{
  "StatusCode": 200,
  "FunctionError": null,
  "Payload": {
    "status": "error",
    "reason": "license_not_found"
  }
}

✅ Lambda invoked successfully
exit=0

$ python3 scripts/simulate_event.py --seller-account 999988887777 --live --function-name mppo-tf-grants-handler   # not allow-listed
Lambda invoke response:
{
  "StatusCode": 200,
  "FunctionError": null,
  "Payload": {
    "status": "skipped",
    "reason": "seller_not_allowed"
  }
}

✅ Lambda invoked successfully
exit=0

$ aws logs filter-log-events --log-group-name /aws/lambda/mppo-tf-grants-handler --filter-pattern "?Step ?\"Notification sent\" ?\"License not found\""
[INFO] 2026-10-22T00:33:28.421Z e6efc65d-edf5-4c7d-8b92-315e0d97918b Step 1: Verifying proposer 444455556666
[INFO] 2026-10-22T00:33:28.575Z e6efc65d-edf5-4c7d-8b92-315e0d97918b Step 2: Discovering license for agreement agmt-2f8c84f429a0470aa09d39d0
[INFO] 2026-10-22T00:33:28.885Z e6efc65d-edf5-4c7d-8b92-315e0d97918b License not found yet (attempt 1/5), waiting 2.0s...
[INFO] 2026-10-22T00:33:31.009Z e6efc65d-edf5-4c7d-8b92-315e0d97918b License not found yet (attempt 2/5), waiting 4.0s...
[INFO] 2026-10-22T00:33:35.083Z e6efc65d-edf5-4c7d-8b92-315e0d97918b License not found yet (attempt 3/5), waiting 8.0s...
[INFO] 2026-10-22T00:33:43.172Z e6efc65d-edf5-4c7d-8b92-315e0d97918b License not found yet (attempt 4/5), waiting 16.0s...
[ERROR] 2026-10-22T00:33:59.243Z e6efc65d-edf5-4c7d-8b92-315e0d97918b License not found after 5 attempts for proposer 444455556666 (issuer Synthetic Issuer mppo-tf E2E)
[INFO] 2026-10-22T00:33:59.354Z e6efc65d-edf5-4c7d-8b92-315e0d97918b Notification sent: MPPO Grant FAILED - License Not Found (Synthetic Seller (mppo-tf E2E))
[INFO] 2026-10-22T00:33:59.862Z ccaaaeb4-7c0d-4aba-a3c2-b085cb907b4c Step 1: Verifying proposer 999988887777
[INFO] 2026-10-22T00:33:59.897Z ccaaaeb4-7c0d-4aba-a3c2-b085cb907b4c Notification sent: MPPO Event - Seller Not in Allow-List
```

## 6. Activation retry rule

The handler is invoked with the retry rule's target `Input`, read back from EventBridge. With an empty table nothing is processed. With a synthetic pending grant, the handler scans the table, calls License Manager `GetGrant`, and records the attempt. `GetGrant` rejects the made-up grant with a validation error rather than `AccessDenied`, so the call itself was authorized.

```text
$ aws events list-targets-by-rule --rule mppo-tf-grant-activation-retry
{
    "Targets": [
        {
            "Id": "terraform-tjHXtfgtrw0dIQFA84p8YVqgaW",
            "Arn": "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:mppo-tf-grants-handler",
            "Input": "{\"detail\":{},\"detail-type\":\"Retry Pending MPPO Grant Activations\",\"source\":\"mppo-grants-automation\"}"
        }
    ]
}

# 1) Retry with an empty pending table, using the rule target Input verbatim
$ aws lambda invoke --function-name mppo-tf-grants-handler --payload file://retry-input.json out.json && cat out.json
{
    "StatusCode": 200,
    "ExecutedVersion": "$LATEST"
}
{"status": "success", "processed": 0, "completed": 0, "stillPending": 0, "failures": 0}

# 2) Record a synthetic pending grant (well-formed ARN, nonexistent grant), then retry again
$ aws dynamodb put-item --table-name mppo-tf-pending-grants --item {"grantArn": "arn:aws:license-manager::ACCOUNT_ID:grant:g-0123456789abcdef0123456789abcdef", ...}
put-item ok
$ aws lambda invoke --function-name mppo-tf-grants-handler --payload file://retry-input.json out.json && cat out.json
{
    "StatusCode": 200,
    "ExecutedVersion": "$LATEST"
}
{"status": "success", "processed": 1, "completed": 0, "stillPending": 1, "failures": 0}
$ aws dynamodb scan --table-name mppo-tf-pending-grants --projection-expression "lastStatus, retryCount"
{
    "Items": [
        {
            "lastStatus": {
                "S": "ACTIVATION_PENDING"
            },
            "retryCount": {
                "N": "1"
            }
        }
    ],
    "Count": 1,
    "ScannedCount": 1,
    "ConsumedCapacity": null
}
$ aws logs filter-log-events --log-group-name /aws/lambda/mppo-tf-grants-handler --filter-pattern "?\"pending grant\" ?\"Activation attempt\" ?\"did not complete\""
[INFO] 2026-10-22T00:33:44.056Z 62efcf4f-b610-488f-8e61-89b5ac118732 Processing scheduled pending grant activation retry
[INFO] 2026-10-22T00:34:14.618Z d24fd781-df0a-43a9-972f-6c2aea76e3a9 Processing scheduled pending grant activation retry
[INFO] 2026-10-22T00:34:16.822Z 28a4d776-119d-42fb-9f62-b71ecc9d652d Processing scheduled pending grant activation retry
[WARNING] 2026-10-22T00:34:17.178Z 28a4d776-119d-42fb-9f62-b71ecc9d652d Activation attempt 1 failed: ValidationException - Invalid grant ARN arn:aws:license-manager::ACCOUNT_ID:grant:g-0123456789abcdef0123456789abcdef.
[WARNING] 2026-10-22T00:34:17.363Z 28a4d776-119d-42fb-9f62-b71ecc9d652d Activation attempt 2 failed: ValidationException - Invalid grant ARN arn:aws:license-manager::ACCOUNT_ID:grant:g-0123456789abcdef0123456789abcdef.
[WARNING] 2026-10-22T00:34:17.535Z 28a4d776-119d-42fb-9f62-b71ecc9d652d Activation attempt 3 failed: ValidationException - Invalid grant ARN arn:aws:license-manager::ACCOUNT_ID:grant:g-0123456789abcdef0123456789abcdef.
[WARNING] 2026-10-22T00:34:17.535Z 28a4d776-119d-42fb-9f62-b71ecc9d652d Grant activation did not complete after 3 attempts; last status: UNKNOWN
```

## 7. EventBridge event pattern

The deployed pattern matches `tests/fixtures/sample-event.json`, rejects nearby detail types and sources, and is identical to the CDK stack's rule. Both rules invoke only their own function.

```text
$ aws events describe-rule --name mppo-tf-agreement-created --query "{State:State,EventPattern:EventPattern}"
{
    "State": "ENABLED",
    "EventPattern": "{\"detail-type\":[\"Purchase Agreement Created - Acceptor\"],\"source\":[\"aws.agreement-marketplace\"]}"
}

# Parity with the deployed CDK rule (read-only)
$ aws events describe-rule --name mppo-agreement-created --query EventPattern
{"detail-type":["Purchase Agreement Created - Acceptor"],"source":["aws.agreement-marketplace"]}
patterns equal: True

$ aws events test-event-pattern --event-pattern "$PATTERN" --event file://tests/fixtures/sample-event.json
{
    "Result": true
}
$ aws events test-event-pattern --event-pattern "$PATTERN" --event <sample-event.json with detail-type="Purchase Agreement Created - Proposer">
{
    "Result": false
}
$ aws events test-event-pattern --event-pattern "$PATTERN" --event <sample-event.json with detail-type="Purchase Agreement Ended - Acceptor">
{
    "Result": false
}
$ aws events test-event-pattern --event-pattern "$PATTERN" --event <sample-event.json with source="aws.marketplace">
{
    "Result": false
}

$ aws events list-targets-by-rule --rule mppo-tf-agreement-created --query "Targets[].Arn"
[
    "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:mppo-tf-grants-handler"
]
$ aws events describe-rule --name mppo-tf-grant-activation-retry --query "{State:State,Schedule:ScheduleExpression}"
{
    "State": "ENABLED",
    "Schedule": "rate(6 hours)"
}
$ aws lambda get-policy --function-name mppo-tf-grants-handler   # Sid, principal, source ARN
AllowActivationRetryRule events.amazonaws.com arn:aws:events:us-east-1:ACCOUNT_ID:rule/mppo-tf-grant-activation-retry
AllowAgreementCreatedRule events.amazonaws.com arn:aws:events:us-east-1:ACCOUNT_ID:rule/mppo-tf-agreement-created
```

## 8. Auto-accept

The synthetic seller opts in with a profile ID that matches no real seller. `ListPurchaseOptions` returns no offers, so nothing is accepted.

```text
$ aws events describe-rule --name mppo-tf-auto-accept-schedule --query "{State:State,Schedule:ScheduleExpression}"
{
    "State": "ENABLED",
    "Schedule": "rate(1 day)"
}
$ aws events list-targets-by-rule --rule mppo-tf-auto-accept-schedule --query "Targets[].Arn"
[
    "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:mppo-tf-auto-accept-handler"
]

$ aws lambda invoke --function-name mppo-tf-auto-accept-handler --payload "{}" out.json && cat out.json
{
    "StatusCode": 200,
    "ExecutedVersion": "$LATEST"
}
{"status": "completed", "checked": 1, "accepted": 0, "unauthorized": 0, "unconfigured": 0, "errors": 0}

$ aws logs filter-log-events --log-group-name /aws/lambda/mppo-tf-auto-accept-handler --filter-pattern "?INFO ?WARNING ?ERROR"
[INFO] 2026-10-22T00:34:45.769Z 937986da-7349-4fa5-b568-4a577c160bbc Auto-accept check starting
[INFO] 2026-10-22T00:34:47.312Z 937986da-7349-4fa5-b568-4a577c160bbc Checking private offers for 1 auto-accept seller(s): Synthetic Seller (mppo-tf E2E)
```

## 9. Schedules, permissions, and notifications

The grant handler ran 5 times against 4 manual invokes. The extra run is the retry rule firing by itself (00:33:44 in step 6's log, before the manual retries), which shows the schedule, target, and invoke permission working end to end. The daily auto-accept schedule did not fire during the run, so its only invocation is the manual one in step 8.

```text
# No authorization failures in either function
$ aws logs filter-log-events --log-group-name /aws/lambda/mppo-tf-grants-handler --filter-pattern '?AccessDenied ?AccessDeniedException ?UnauthorizedOperation ?"not authorized"' --query 'events[].message'
[]
$ aws logs filter-log-events --log-group-name /aws/lambda/mppo-tf-auto-accept-handler --filter-pattern '?AccessDenied ?AccessDeniedException ?UnauthorizedOperation ?"not authorized"' --query 'events[].message'
[]

# SNS publishes (the topic has no subscriptions). expected 2, both from the handler step: License Not Found and Seller Not in Allow-List
$ aws cloudwatch get-metric-statistics --namespace AWS/SNS --metric-name NumberOfMessagesPublished --dimensions Name=TopicName,Value=mppo-tf-grants-notifications --start-time <run start> --period 60 --statistics Sum --query "sum(Datapoints[].Sum)"
2.0

# Lambda invocations since the run started. Manual invokes: 4 for the grant handler (2 events, 2 retries) and 1 for auto-accept; anything above that came from the EventBridge schedules
$ aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations --dimensions Name=FunctionName,Value=mppo-tf-grants-handler --start-time <run start> --period 60 --statistics Sum --query "sum(Datapoints[].Sum)"
5.0
$ aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations --dimensions Name=FunctionName,Value=mppo-tf-auto-accept-handler --start-time <run start> --period 60 --statistics Sum --query "sum(Datapoints[].Sum)"
1.0
```

## 10. State and drift

```text
$ terraform state list
data.archive_file.lambda
data.aws_iam_policy_document.auto_accept[0]
data.aws_iam_policy_document.chatbot_assume
data.aws_iam_policy_document.handler
data.aws_iam_policy_document.lambda_assume
data.aws_partition.current
aws_cloudwatch_event_rule.activation_retry
aws_cloudwatch_event_rule.agreement_created
aws_cloudwatch_event_rule.auto_accept[0]
aws_cloudwatch_event_target.activation_retry
aws_cloudwatch_event_target.agreement_created
aws_cloudwatch_event_target.auto_accept[0]
aws_cloudwatch_log_group.auto_accept[0]
aws_cloudwatch_log_group.handler
aws_dynamodb_table.allowed_sellers
aws_dynamodb_table.pending_grants
aws_iam_role.auto_accept[0]
aws_iam_role.handler
aws_iam_role_policy.auto_accept[0]
aws_iam_role_policy.handler
aws_lambda_function.auto_accept[0]
aws_lambda_function.handler
aws_lambda_permission.activation_retry
aws_lambda_permission.agreement_created
aws_lambda_permission.auto_accept[0]
aws_sns_topic.notifications
terraform_data.config_checks

$ terraform plan -detailed-exitcode   # re-plan after apply: expect no drift

No changes. Your infrastructure matches the configuration.

Terraform has compared your real infrastructure against your configuration
and found no differences, so no changes are needed.
exit=0
```

## 11. Destroy

The per-attribute diff is left out for length. The resource list and progress lines are shown.

```text
$ terraform destroy -auto-approve
  # aws_cloudwatch_event_rule.activation_retry will be destroyed
  # aws_cloudwatch_event_rule.agreement_created will be destroyed
  # aws_cloudwatch_event_rule.auto_accept[0] will be destroyed
  # aws_cloudwatch_event_target.activation_retry will be destroyed
  # aws_cloudwatch_event_target.agreement_created will be destroyed
  # aws_cloudwatch_event_target.auto_accept[0] will be destroyed
  # aws_cloudwatch_log_group.auto_accept[0] will be destroyed
  # aws_cloudwatch_log_group.handler will be destroyed
  # aws_dynamodb_table.allowed_sellers will be destroyed
  # aws_dynamodb_table.pending_grants will be destroyed
  # aws_iam_role.auto_accept[0] will be destroyed
  # aws_iam_role.handler will be destroyed
  # aws_iam_role_policy.auto_accept[0] will be destroyed
  # aws_iam_role_policy.handler will be destroyed
  # aws_lambda_function.auto_accept[0] will be destroyed
  # aws_lambda_function.handler will be destroyed
  # aws_lambda_permission.activation_retry will be destroyed
  # aws_lambda_permission.agreement_created will be destroyed
  # aws_lambda_permission.auto_accept[0] will be destroyed
  # aws_sns_topic.notifications will be destroyed
  # terraform_data.config_checks will be destroyed
Plan: 0 to add, 0 to change, 21 to destroy.
  (per-attribute diff omitted)

aws_cloudwatch_event_target.auto_accept[0]: Destroying... [id=mppo-tf-auto-accept-schedule-terraform-8d8H7ZGbegKthLYttAWeLxOU74]
aws_cloudwatch_event_target.activation_retry: Destroying... [id=mppo-tf-grant-activation-retry-terraform-tjHXtfgtrw0dIQFA84p8YVqgaW]
aws_lambda_permission.agreement_created: Destroying... [id=AllowAgreementCreatedRule]
aws_lambda_permission.activation_retry: Destroying... [id=AllowActivationRetryRule]
aws_lambda_permission.auto_accept[0]: Destroying... [id=AllowAutoAcceptSchedule]
aws_cloudwatch_event_target.agreement_created: Destroying... [id=mppo-tf-agreement-created-terraform-36gcPdXHBcpZhUZOzJsQsGrU1k]
aws_cloudwatch_event_target.agreement_created: Destruction complete after 0s
aws_cloudwatch_event_target.activation_retry: Destruction complete after 0s
aws_cloudwatch_event_target.auto_accept[0]: Destruction complete after 0s
aws_lambda_permission.auto_accept[0]: Destruction complete after 0s
aws_cloudwatch_event_rule.auto_accept[0]: Destroying... [id=mppo-tf-auto-accept-schedule]
aws_lambda_function.auto_accept[0]: Destroying... [id=mppo-tf-auto-accept-handler]
aws_cloudwatch_event_rule.auto_accept[0]: Destruction complete after 0s
aws_lambda_permission.agreement_created: Destruction complete after 0s
aws_cloudwatch_event_rule.agreement_created: Destroying... [id=mppo-tf-agreement-created]
aws_lambda_permission.activation_retry: Destruction complete after 0s
aws_cloudwatch_event_rule.agreement_created: Destruction complete after 0s
aws_cloudwatch_event_rule.activation_retry: Destroying... [id=mppo-tf-grant-activation-retry]
aws_lambda_function.handler: Destroying... [id=mppo-tf-grants-handler]
aws_cloudwatch_event_rule.activation_retry: Destruction complete after 0s
aws_lambda_function.auto_accept[0]: Destruction complete after 5s
aws_iam_role_policy.auto_accept[0]: Destroying... [id=mppo-tf-auto-accept-handler:mppo-tf-auto-accept-handler]
aws_iam_role_policy.auto_accept[0]: Destruction complete after 0s
aws_cloudwatch_log_group.auto_accept[0]: Destroying... [id=/aws/lambda/mppo-tf-auto-accept-handler]
aws_iam_role.auto_accept[0]: Destroying... [id=mppo-tf-auto-accept-handler]
aws_lambda_function.handler: Destruction complete after 5s
terraform_data.config_checks: Destroying... [id=c2dec23f-9980-abcd-af35-4149b3a45696]
terraform_data.config_checks: Destruction complete after 0s
aws_iam_role_policy.handler: Destroying... [id=mppo-tf-grants-handler:mppo-tf-grants-handler]
aws_cloudwatch_log_group.auto_accept[0]: Destruction complete after 0s
aws_iam_role.auto_accept[0]: Destruction complete after 0s
aws_iam_role_policy.handler: Destruction complete after 0s
aws_cloudwatch_log_group.handler: Destroying... [id=/aws/lambda/mppo-tf-grants-handler]
aws_dynamodb_table.pending_grants: Destroying... [id=mppo-tf-pending-grants]
aws_dynamodb_table.allowed_sellers: Destroying... [id=mppo-tf-allowed-sellers]
aws_sns_topic.notifications: Destroying... [id=arn:aws:sns:us-east-1:ACCOUNT_ID:mppo-tf-grants-notifications]
aws_iam_role.handler: Destroying... [id=mppo-tf-grants-handler]
aws_cloudwatch_log_group.handler: Destruction complete after 0s
aws_sns_topic.notifications: Destruction complete after 0s
aws_iam_role.handler: Destruction complete after 0s
aws_dynamodb_table.pending_grants: Destruction complete after 13s
aws_dynamodb_table.allowed_sellers: Destruction complete after 13s
Destroy complete! Resources: 21 destroyed.
exit=0
```

## 12. Teardown check

```text
# Leftover check: every resource type the module creates, filtered by the mppo-tf prefix (empty = clean)
$ terraform state list | wc -l
0
$ aws lambda list-functions --query "Functions[?starts_with(FunctionName, 'mppo-tf')].FunctionName"
[]
$ aws dynamodb list-tables --query "TableNames[?starts_with(@, 'mppo-tf')]"
[]
$ aws events list-rules --name-prefix mppo-tf --query "Rules[].Name"
[]
$ aws sns list-topics --query "Topics[?contains(TopicArn, ':mppo-tf-')].TopicArn"
[]
$ aws iam list-roles --query "Roles[?starts_with(RoleName, 'mppo-tf')].RoleName"
[]
$ aws logs describe-log-groups --log-group-name-prefix /aws/lambda/mppo-tf --query "logGroups[].logGroupName"
[]
$ aws resourcegroupstaggingapi get-resources --tag-filters Key=ManagedBy,Values=terraform Key=Project,Values=sample-bedrock-managed-entitlements --query "ResourceTagMappingList[].ResourceARN"
[]

# The CDK stack and its resources are untouched
$ aws cloudformation describe-stacks --stack-name MppoGrantsAutomationStack --query "Stacks[0].{Status:StackStatus,LastUpdated:LastUpdatedTime}"   # before apply, then after destroy
{
    "Status": "UPDATE_COMPLETE",
    "LastUpdated": "2026-08-14T12:59:57.993000+00:00"
}
{
    "Status": "UPDATE_COMPLETE",
    "LastUpdated": "2026-08-14T12:59:57.993000+00:00"
}
$ aws lambda list-functions --query "Functions[?starts_with(FunctionName, 'mppo')].FunctionName"
[
    "mppo-grants-handler"
]
$ aws dynamodb list-tables --query "TableNames[?starts_with(@, 'mppo')]"
[
    "mppo-allowed-sellers",
    "mppo-pending-grants"
]
$ aws events list-rules --name-prefix mppo --query "Rules[].Name"
[
    "mppo-agreement-created",
    "mppo-grant-activation-retry"
]
```

## 13. Python test suite

```text
$ python3 -m pytest tests/ -q
........................................................................ [ 77%]
.....................                                                    [100%]
93 passed in 8.02s
```
