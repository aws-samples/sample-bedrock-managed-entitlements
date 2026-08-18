# Scripts

Utility scripts for deployment, testing, and discount verification.

## Overview

| Script | Run From | Purpose |
|--------|----------|---------|
| `setup_config.py` | Management account | **Start here** — interactive setup that auto-discovers org ID and licenses |
| `bootstrap_prereqs.py` | Management account | Check and optionally enable License Manager and Marketplace organization prerequisites |
| `seed_sellers.py` | Management account | Populate DynamoDB with allowed sellers after `cdk deploy` |
| `backfill_grants.py` | Management account | Backfill grants for existing received licenses, scoped to the allow-list |
| `distribute_licenses.py` | Management account | **Lightweight alternative** — backfill every received license (no allow-list, no config file); same dry-run/`--apply` review gate |
| `simulate_event.py` | Management account | Generate test events and invoke the handler locally or deployed Lambda |
| `e2e_validate.py` | Management account | Validate all infrastructure components are correctly deployed |
| `create_grant_manual.py` | Management account | Create grants for existing subscriptions (backfill) |
| `bedrock_discount_check.py` | Member account OR management account | Verify negotiated pricing is actually flowing |

## Lightweight Alternative: `distribute_licenses.py`

`backfill_grants.py` is allow-list scoped: it only touches licenses matching sellers already in `config/sellers.json`, and refuses broad issuer-only matches. That's the right default, but it means you have to maintain a config file even for a one-off bootstrap.

`distribute_licenses.py` is the lightweight equivalent for when you don't want that config file at all — for example, right after deploying, when you already trust every license currently sitting in `ListReceivedLicenses` and just want them all distributed and activated in one pass. It has **no allow-list**: every non-expired received license is in scope. To offset that, it keeps the same review gate as `backfill_grants.py`:

```bash
# 1. Dry-run: lists what would be distributed and activated. No mutating API calls.
python3 scripts/distribute_licenses.py

# 2. Review the printed plan, then apply:
python3 scripts/distribute_licenses.py --apply --confirm-account-id 123456789012
```

`--confirm-account-id` must match the AWS account you're actually running in (same pattern `backfill_grants.py` and `bootstrap_prereqs.py` use) — this is a guard against accidentally running an org-wide grant operation against the wrong account.

What it does on `--apply`:

1. Lists every received License Manager license (`ListReceivedLicenses`), skipping `EXPIRED`/`DELETED`.
2. Discovers your organization ARN (`DescribeOrganization`) and uses it as the grant principal.
3. Creates an org-wide grant for each license (`CreateGrant`) — re-runs are idempotent; an already-distributed license reuses its existing grant instead of erroring.
4. Polls the grant until distribution finishes (`GetGrant` → `WORKFLOW_COMPLETED`).
5. Activates the grant (`CreateGrantVersion(Status=ACTIVE)`), fixing the same Disabled→Active gotcha the CDK automation and `backfill_grants.py` handle.

**Choosing between the three:**

| | `distribute_licenses.py` | `backfill_grants.py` | CDK automation |
|---|---|---|---|
| Config file required | No | Yes (`config/sellers.json`) | Yes |
| Allow-list scoped | No — every received license | Yes | Yes |
| Handles new offers going forward | No — one-shot, re-run manually | No — one-shot, re-run manually | Yes, automatically |
| Infra deployed | None | None | EventBridge + Lambda + DynamoDB |
| Review step before mutating | Dry-run by default, `--apply` required | Dry-run by default, `--apply` required | N/A (automatic) |
| Best for | Fastest path when you trust everything currently received | One-off backfill scoped to sellers you've already vetted | Ongoing, hands-off automation |

**Must run from the management account** (same requirement as the other two — only the management account can create org-wide grants).

**IAM requirements (no infra to provision — grant management + org lookup only):**

```yaml
- license-manager:ListReceivedLicenses
- license-manager:ListDistributedGrants
- license-manager:CreateGrant
- license-manager:CreateGrantVersion
- license-manager:GetGrant
- organizations:DescribeOrganization
- sts:GetCallerIdentity
```

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

# Dry-run existing received licenses before creating backfill grants
python3 scripts/backfill_grants.py --config config/sellers.json

# Apply a specific existing license after reviewing the dry-run
python3 scripts/backfill_grants.py \
    --config config/sellers.json \
    --license-arn arn:aws:license-manager::123456789012:license:l-example \
    --apply \
    --confirm-account-id 123456789012

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
