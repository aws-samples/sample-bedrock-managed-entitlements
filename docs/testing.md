# Testing and validation

## Verifying Discounts

After grants are activated, verify the negotiated rate is flowing:

```bash
# From member account (CloudShell - zero setup)
python3 scripts/bedrock_discount_check.py --issuer "Anthropic, PBC"

# From management account (scope billing to a member)
python3 scripts/bedrock_discount_check.py --issuer "Anthropic, PBC" --linked-account 222233334444
```

See [`scripts/README.md`](../scripts/README.md) for full usage.

## Testing Without Live Offers

```bash
# Unit tests (mocked AWS services)
pip install -r requirements-dev.txt && pytest tests/ -v

# Terraform plan assertions (offline, mocked AWS provider)
terraform -chdir=terraform init && terraform -chdir=terraform test

# Invoke the handler locally with an EventBridge-shaped payload
python scripts/simulate_event.py --seller-account 123456789012

# Invoke the deployed Lambda with an EventBridge-shaped payload
python scripts/simulate_event.py --seller-account 123456789012 --live
```

## E2E Validation

```bash
# Validate all deployed infrastructure
python scripts/e2e_validate.py --org-id o-xxxxxxxxxx --seller-account 444455556666 --simulate
```

## Validation Status

| Layer | Status | Method |
|-------|--------|--------|
| EventBridge event schema | ✅ Validated | Matched against [official AWS docs](https://docs.aws.amazon.com/marketplace/latest/buyerguide/agreement-eventbridge.html) |
| License Manager API calls | ✅ Validated | `CreateGrant`, `CreateGrantVersion` verified against [API Reference](https://docs.aws.amazon.com/license-manager/latest/APIReference/); grant activation live-tested against an ISV Partner private offer |
| Marketplace Discovery API calls | ✅ Validated | `ListPurchaseOptions`, `GetOffer`, `GetOfferTerms` live-tested against the real API (params, IAM permissions) |
| Marketplace Agreement accept calls | ⚠️ Shape-validated | `CreateAgreementRequest`, `AcceptAgreementRequest` verified against API Reference and IAM policy simulation; not yet exercised against a live unaccepted offer |
| Lambda handler logic | ✅ Validated | Unit and integration tests (moto + fakes) |
| CDK infrastructure | ✅ Validated | CDK assertion tests, incl. auto-accept Lambda when `enableAutoAccept: true` |
| Terraform infrastructure | ✅ Validated | Offline `terraform test` plan assertions and checkov; live apply of the full stack with a synthetic seller, including the entitlement monitor: monitor and retry invocations with their scheduled inputs, the reconciliation heartbeat metric, error and heartbeat alarms wired to the SNS topic, and a clean destroy |
