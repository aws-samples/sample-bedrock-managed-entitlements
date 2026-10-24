# Auto-Accept Offers (Optional)

⚠️ **RISK: Auto-accept creates financial commitments automatically.** Only enable for sellers with pre-negotiated terms you are comfortable accepting without manual review.

When enabled, a scheduled Lambda lists private offers visible to this account via the [Marketplace Discovery API](https://docs.aws.amazon.com/marketplace/latest/developerguide/use-apis-as-buyer.html) (`ListPurchaseOptions` + `GetOffer` + `GetOfferTerms`), matches them against trusted sellers in the allow-list, and accepts matching offers via the Marketplace Agreement API (`CreateAgreementRequest` + `AcceptAgreementRequest`). The existing grant automation then distributes the license.

> Unaccepted private offers aren't modeled as agreements in the Marketplace Agreement API — an agreement only exists after acceptance. Discovery therefore goes through `marketplace-discovery`, not `SearchAgreements`.

**Two-level opt-in required:**
1. Global: `"enableAutoAccept": true` in config (deploys the Lambda)
2. Per-seller: `"autoAcceptOffers": true` plus a `sellerProfileId` (opts in that seller)

```json
{
  "enableAutoAccept": true,
  "autoAcceptSchedule": "rate(1 hour)",
  "allowedSellers": [{
    "name": "Anthropic",
    "proposerAccountId": "123456789012",
    "sellerProfileId": "prof-xxxxxxxxxxxx",
    "autoAcceptOffers": true
  }]
}
```

**How offers are authorized.** `GetOffer` identifies a seller by two fields: `sellerProfileId`, a unique AWS-assigned identifier, and `displayName`, a seller-chosen human-readable string. Display names are not unique, so authorization keys exclusively on `sellerProfileId` — `displayName` is used only in logs and notifications.

Trusted profile IDs are passed to `ListPurchaseOptions` as a `SELLER_OF_RECORD_PROFILE_ID` filter so offers from other sellers are never returned, then re-checked after `GetOffer`. A seller with `autoAcceptOffers` but no `sellerProfileId` never auto-accepts; the Lambda notifies admins instead of falling back to a name match.

Find a seller's profile ID on the offer in the AWS Marketplace console, then add it to `config/sellers.json` and re-run `scripts/seed_sellers.py`.

> The seller's AWS account ID isn't available before acceptance — `sellerOfRecord` doesn't carry one. After each acceptance the Lambda calls `DescribeAgreement` and compares `proposer.accountId` against the record's `proposerAccountId`, alerting on a mismatch as an audit signal (the agreement already exists by then, so this confirms rather than blocks).

**When to use:** Ongoing relationship with seller, consistent terms across models, budget pre-approved.
**When NOT to use:** Variable pricing, compliance requires manual approval, offer terms may change.
