# Lightweight Path: `distribute_licenses.py`

This folder is a **self-contained alternative** to the CDK automation in this repo. If you just want to fix License Manager's "Disabled → Active" billing gotcha for licenses you already have, without deploying any infrastructure, this is the fastest way to do it.

**What you get:** one script, `boto3` only, no config file, no CDK, no Lambda/DynamoDB/EventBridge.

**What you give up:** it isn't scoped to an allow-list, and it doesn't react automatically to new private offers going forward. For that, see the [full CDK automation](../README.md) or its allow-list-scoped backfill script, [`../scripts/backfill_grants.py`](../scripts/backfill_grants.py).

If you're not sure which path is right for you, see [Choose Your Path](../README.md#choose-your-path) in the main README.

## Usage

Run from your AWS Organizations **management account**, `us-east-1`:

```bash
# 1. Dry-run: lists every received license and what would happen. No mutating API calls.
python3 lightweight/distribute_licenses.py

# 2. Review the printed plan, then apply:
python3 lightweight/distribute_licenses.py --apply --confirm-account-id 123456789012
```

`--confirm-account-id` must match the AWS account you're actually running in — a guard against accidentally running an org-wide grant operation against the wrong account (same pattern used by `bootstrap_prereqs.py` and `backfill_grants.py` elsewhere in this repo).

## What it does (on `--apply`)

1. Lists every received License Manager license (`ListReceivedLicenses`), skipping `EXPIRED`/`DELETED`.
2. Discovers your organization ARN (`DescribeOrganization`) and uses it as the grant principal.
3. Creates an org-wide grant for each license (`CreateGrant`) — re-runs are idempotent; an already-distributed license reuses its existing grant instead of erroring.
4. Polls the grant until distribution finishes (`GetGrant` → `WORKFLOW_COMPLETED`).
5. Activates the grant (`CreateGrantVersion(Status=ACTIVE)`), fixing the Disabled→Active gotcha.

## Important: no allow-list

Unlike everything else in this repo, `distribute_licenses.py` does **not** check received licenses against a seller allow-list. Every non-expired license currently in `ListReceivedLicenses` is in scope. That's what makes it lightweight — no `config/sellers.json` to create or maintain — but it also means the dry-run plan printed in step 1 is your only review step. Read it before passing `--apply`.

If you want per-seller scoping, use [`../scripts/backfill_grants.py`](../scripts/backfill_grants.py) instead.

## IAM requirements

Read + grant-management only, no infrastructure to provision:

```yaml
- license-manager:ListReceivedLicenses
- license-manager:ListDistributedGrants
- license-manager:CreateGrant
- license-manager:CreateGrantVersion
- license-manager:GetGrant
- organizations:DescribeOrganization
- sts:GetCallerIdentity
```

## Which path is right for me?

| | `lightweight/distribute_licenses.py` | `scripts/backfill_grants.py` | Full CDK automation |
|---|---|---|---|
| Config file required | No | Yes (`config/sellers.json`) | Yes |
| Allow-list scoped | No — every received license | Yes | Yes |
| Handles new offers going forward | No — one-shot, re-run manually | No — one-shot, re-run manually | Yes, automatically |
| Infra deployed | None | None | EventBridge + Lambda + DynamoDB |
| Review step before mutating | Dry-run by default, `--apply` required | Dry-run by default, `--apply` required | N/A (automatic) |
| Best for | Fastest path when you trust everything currently received | One-off backfill scoped to sellers you've already vetted | Ongoing, hands-off automation |
