# Security

## Vulnerability Reporting

If you discover a security vulnerability in this sample, please report it via [GitHub Issues](https://github.com/wirjo/sample-bedrock-managed-entitlements/issues) or contact the maintainer directly.

## Shared Responsibility

This is a sample implementation. You are responsible for reviewing, adapting, and securing it for your environment. See the [Security section in README](README.md#security) for the full shared responsibility breakdown.

## Security Scan Results

Last scanned: 2026-08-01 using [AWS Automated Security Helper (ASH)](https://github.com/awslabs/automated-security-helper)

| Scanner | Result | Notes |
|---------|--------|-------|
| CDK-Nag | ✅ CLEAN | No CDK best-practice violations |
| CFN-Nag | ✅ CLEAN | No CloudFormation security warnings |
| Checkov | ✅ CLEAN (0 findings) | Infrastructure-as-code policy checks |
| Opengrep | ✅ CLEAN | Code pattern analysis |
| Semgrep | ✅ CLEAN | Static analysis for security anti-patterns |
| Bandit | ✅ 18 note-level | All `assert` usage in test files (expected for pytest) |
| detect-secrets | ✅ 3 false positives | UUID strings in test fixtures (baselined in `.secrets.baseline`) |
| Grype | ⚠️ Incomplete | Dependency vulnerability scan (requires >2GB RAM) |

### False Positives

The `.secrets.baseline` file suppresses known false positives from detect-secrets:
- Test fixture UUIDs in `tests/fixtures/sample-event.json`
- Mock account IDs in `tests/test_handler.py`

These are synthetic test data, not real credentials.

## Re-running the Security Scan

```bash
# Clone ASH
git clone https://github.com/awslabs/automated-security-helper.git /tmp/ash

# Run against this repo
cd /tmp/ash && bash ash --source-dir /path/to/sample-bedrock-managed-entitlements --output-dir /tmp/ash-output --force

# Results are in /tmp/ash-output/scanners/*/
```

Requires: Docker or local Python environment with scanner dependencies. See [ASH documentation](https://github.com/awslabs/automated-security-helper#readme) for prerequisites.

## IAM Least Privilege

All Lambda functions use scoped IAM policies:

- **Grant handler**: Read-only DynamoDB access, License Manager create/activate grants only, SNS publish to a single topic
- **Auto-accept handler** (if enabled): Marketplace agreement search/accept only, read-only DynamoDB, SNS publish

No wildcard resource policies are used except where AWS APIs require `Resource: *` (e.g., `sts:GetCallerIdentity`, `organizations:DescribeOrganization`).
