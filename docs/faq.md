# FAQ

**Is a subscription per model?** Yes. Each model needs its own subscription. This automation handles the grant step, but per-model subscription is inherent.

**Which account do I deploy from?** The management account. Only it can create org-wide grants.

**CDK or Terraform?** Either. Pick the one your team already uses. See [Choose CDK or Terraform](../README.md#choose-cdk-or-terraform).

**What if my org uses "consolidated billing" only?** You're limited to account-level grants. Enable "all features" in Organizations.

**What about old per-account grants?** Set `replaceLegacyGrants: true` to auto-replace them.

**How do I verify the discount is working?** Run `scripts/bedrock_discount_check.py`.
