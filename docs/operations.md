# Operations

Day-two tasks: bringing existing licenses under management, replacing legacy grants, troubleshooting manual activation, and cost.

## Backfill Existing Licenses

EventBridge automation handles new Marketplace agreement events. For licenses accepted before you deployed this sample, you have two options depending on how much control you want:

**Allow-list scoped (recommended default):** reuses your `config/sellers.json` allow-list, so only licenses from sellers you've already vetted are touched.

```bash
python3 scripts/backfill_grants.py --config config/sellers.json
```

Backfill is allow-list scoped and dry-run by default. Because License Manager received licenses do not reliably expose the Marketplace proposer account ID, the script refuses broad issuer-only matching. Narrow the run by passing explicit `--license-arn` values, or add `productSkus` or `productNames` to the relevant seller config.

```bash
python3 scripts/backfill_grants.py \
  --config config/sellers.json \
  --license-arn arn:aws:license-manager::123456789012:license:l-example \
  --apply \
  --confirm-account-id 123456789012
```

**Lightweight, no config file:** if you don't want to maintain `config/sellers.json` at all — e.g. a one-off bootstrap where you're comfortable distributing every Bedrock-matching received license — use the standalone script in [`lightweight/`](../lightweight/) instead of anything in this section. See [Choose Your Path](../README.md#choose-your-path) and [`lightweight/README.md`](../lightweight/README.md) for full usage; it doesn't require anything else in this repo.

## Legacy Offer Cleanup

Set `"replaceLegacyGrants": true` to automatically disable old per-account grants when activating the new org-wide grant. Uses `ActivationOverrideBehavior: ALL_GRANTS_PERMITTED_BY_ISSUER`.

Default (`false`): new grant activates without affecting existing grants.

## Troubleshoot manual grant activation

If selecting **Activate** in the License Manager console does not advance to the
confirmation screen, verify that the operator's IAM policy allows these actions:

```yaml
- license-manager:AcceptGrant
- license-manager:GetLicense
- license-manager:GetLicenseUsage
```

`license-manager:List*` does not include the two `Get*` actions. Missing read
permissions can make the console appear unresponsive instead of showing a clear
authorization error. Check AWS CloudTrail for `AccessDenied` events from
`GetLicense` or `GetLicenseUsage`, then add only the missing actions to the
operator's policy and retry activation.

These permissions apply to a person manually accepting and activating a received
grant. The sample also includes them in the Lambda execution role so the deployed
License Manager policy covers both grant management paths. A console operator
still needs the permissions attached to their own IAM principal.

## Cost Estimate

The deployment costs effectively **$0/month at rest**, whichever tool you use. You only pay when a private offer event fires.

| Resource | Cost | Notes |
|----------|------|-------|
| Lambda (grant handler) | ~$0 | Free tier: 1M requests/month. Invoked only on new agreements (rare). |
| Lambda (auto-accept, if enabled) | ~$0 | One invocation per schedule interval (e.g., hourly = 720/month). |
| DynamoDB (seller allow-list) | ~$0 | On-demand pricing. Single-digit items, minimal reads. |
| EventBridge | $0 | No charge for AWS service events or rules. |
| SNS | ~$0 | Free tier: 1,000 email notifications/month. |
| AWS Chatbot (Slack) | $0 | No additional charge. |
| CloudWatch Logs | ~$0.50 | 2-week retention. Minimal log volume. |

**Estimated total: < $1/month** under normal usage (a few private offers per month).

No NAT Gateways, no VPCs, no always-on compute. The deployment is entirely serverless and event-driven.
