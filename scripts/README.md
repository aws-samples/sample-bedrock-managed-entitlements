# Scripts

Utility scripts for deployment, testing, and discount verification.

## Overview

| Script | Run From | Purpose |
|--------|----------|---------|
| `setup_config.py` | Management account | **Start here** — interactive setup that auto-discovers org ID and licenses |
| `bootstrap_prereqs.py` | Management account | Check and optionally enable License Manager and Marketplace organization prerequisites |
| `seed_sellers.py` | Management account | Populate DynamoDB with allowed sellers after `cdk deploy` |
| `simulate_event.py` | Management account | Generate test events and invoke the handler locally or deployed Lambda |
| `e2e_validate.py` | Management account | Validate all infrastructure components are correctly deployed |
| `create_grant_manual.py` | Management account | Create grants for existing subscriptions (backfill) |
| `bedrock_discount_check.py` | Member account OR management account | Verify negotiated pricing is actually flowing |

## Where to Run Each Script

### Management Account Scripts

These scripts deploy, configure, or test the automation infrastructure. Run them from the **management account** in `us-east-1`:

```bash
# Check prerequisite services without making changes
python3 scripts/bootstrap_prereqs.py --check --region us-east-1

# Review the output, then apply org-wide prerequisites only after confirming the target account
python3 scripts/bootstrap_prereqs.py \
    --apply \
    --region us-east-1 \
    --confirm-account-id 123456789012

# Optional: register a License Manager delegated admin only with explicit confirmation
python3 scripts/bootstrap_prereqs.py \
    --apply \
    --region us-east-1 \
    --confirm-account-id 123456789012 \
    --delegated-admin-account-id 222233334444 \
    --confirm-delegated-admin-account-id 222233334444

# After cdk deploy — seed the seller allow-list
python3 scripts/seed_sellers.py --config config/sellers.json

# Validate all infra components
python3 scripts/e2e_validate.py --org-id o-xxxxxxxxxx --seller-account 444455556666

# Invoke the deployed Lambda with a synthetic Marketplace event
python3 scripts/simulate_event.py --seller-account 444455556666 --live

# Create a grant for a pre-existing subscription
python3 scripts/create_grant_manual.py \
    --license-arn arn:aws:license-manager::123456789012:license:lic-xxx \
    --org-id o-xxxxxxxxxx
```

### Discount Verification Script

`bedrock_discount_check.py` verifies that the negotiated rate is actually applying. It can be run from **either**:

**Option A: Member account (recommended for grant status)**

Run directly in the member account that makes Bedrock API calls. Uses ambient credentials — paste into AWS CloudShell for zero-setup:

```bash
python3 scripts/bedrock_discount_check.py --issuer "Anthropic, PBC"
```

This checks the grant status in *that specific account* and confirms the discount is flowing there.

**Option B: Management/payer account (recommended for billing)**

The management account has Cost Explorer access across all member accounts. Use `--linked-account` to scope the billing check:

```bash
python3 scripts/bedrock_discount_check.py \
    --issuer "Anthropic, PBC" \
    --linked-account 222233334444
```

**When to use which:**

| Scenario | Run From | Why |
|----------|----------|-----|
| Check if grants are active in a specific account | Member account | Sees that account's received grants directly |
| Check billing rates across multiple accounts | Management account | Cost Explorer access to all linked accounts |
| Quick CloudShell check after activation | Member account | Zero setup, ambient creds |
| Bulk verification across the org | Management account | Loop over `--linked-account` for each |

### Discount Checker Options

```bash
# Auto-derives activation date from license validity
python3 scripts/bedrock_discount_check.py --issuer "Anthropic, PBC"

# Override activation date manually
python3 scripts/bedrock_discount_check.py --issuer "Anthropic, PBC" --activation-date 2026-06-01

# Grants only, skip billing check
python3 scripts/bedrock_discount_check.py --no-billing --issuer "Anthropic, PBC"

# Flag specific models you expect to be present
python3 scripts/bedrock_discount_check.py --no-billing --expect "Claude Opus 4,Claude Sonnet 4"

# Machine-readable output
python3 scripts/bedrock_discount_check.py --json report.json
```

### IAM Requirements for Discount Checker

Read-only permissions needed:
- `license-manager:ListReceivedLicenses`
- `license-manager:ListReceivedGrants`
- `ce:GetCostAndUsage` (billing proof only)
- `sts:GetCallerIdentity`
