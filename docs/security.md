# Security

## Shared Responsibility Model

This sample deploys infrastructure into **your** AWS account. Under the [AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/):

| Responsibility | Owner |
|---------------|-------|
| Infrastructure security (compute, network, storage) | AWS |
| IAM policies and access control | You |
| Configuration of the allow-list (which sellers to trust) | You |
| Enabling/disabling auto-accept and understanding its risks | You |
| Monitoring grant status and billing rates | You |
| Secrets management and credential rotation | You |
| Compliance with your organization's procurement policies | You |

**This is a sample** - review and adapt it to your organization's security requirements before production use.

## IAM Permissions (Least Privilege)

```yaml
# Grant distribution Lambda
- license-manager:ListReceivedLicenses
- license-manager:AcceptGrant
- license-manager:GetLicense
- license-manager:GetLicenseUsage
- license-manager:CreateGrant
- license-manager:CreateGrantVersion
- license-manager:GetGrant
- license-manager:ListDistributedGrants
- aws-marketplace:DescribeAgreement
- aws-marketplace:SearchAgreements
- dynamodb:GetItem (config table only)
- sns:Publish (notification topic only)
- sts:GetCallerIdentity
- organizations:DescribeOrganization
- organizations:ListAccounts

# Auto-accept Lambda (only deployed if enabled)
- aws-marketplace:ListPurchaseOptions
- aws-marketplace:GetOffer
- aws-marketplace:GetOfferTerms
- aws-marketplace:CreateAgreementRequest
- aws-marketplace:AcceptAgreementRequest
- aws-marketplace:DescribeAgreement
```

## Key Security Considerations

- The **DynamoDB allow-list** is the gatekeeper - only sellers in this table trigger automation
- Seller identity is matched on **AWS-assigned IDs** (`proposerAccountId` for grants, `sellerProfileId` for auto-accept), never on a seller-supplied display name
- **Auto-accept** is disabled by default and requires two explicit opt-ins
- All actions are logged to **CloudTrail** for audit
- A scheduled reconciliation detects unaccepted offers and missing or inactive grants
- Lambda error and heartbeat alarms report failed or missing monitor runs
- SNS defaults to exception-only digests to avoid one email per account and model
- The Lambda has **no write access** to License Manager beyond grant creation/activation
