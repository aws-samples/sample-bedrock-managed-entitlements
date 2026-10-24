# Automate model subscriptions with managed entitlements for Amazon Bedrock

[![Deploy with AWS CDK](https://img.shields.io/badge/AWS_CDK-deploy-FF9900?style=flat-square)](#deploy-with-aws-cdk)
[![Deploy with Terraform](https://img.shields.io/badge/Terraform-deploy-844FBA?logo=terraform&logoColor=white&style=flat-square)](#deploy-with-terraform)
[![View the slides](https://img.shields.io/badge/Slides-view_the_deck-232F3E?logo=googleslides&logoColor=white&style=flat-square)](https://aws-samples.github.io/sample-bedrock-managed-entitlements/slides/)

A sample implementation that automates the distribution of [managed entitlements for Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/managed-entitlements.html) across your AWS Organization. Deploy this in your management account to automatically create and activate License Manager grants whenever a new Marketplace private offer is accepted - eliminating manual per-account grant management.

Deploy it with **AWS CDK** or **Terraform**. Both deploy the same resources from the same `config/sellers.json` and the same Lambda code.

**[View the slides](https://aws-samples.github.io/sample-bedrock-managed-entitlements/slides/)** for a walkthrough of managed entitlements for Amazon Bedrock and this sample.

> **This is a sample.** It is intended as a starting point for customers to review, adapt, and extend to meet their own security, compliance, and operational requirements. See [Security](docs/security.md) for shared responsibility guidance.

## Challenge

When you negotiate private pricing for a model through an [AWS Marketplace private offer](https://docs.aws.amazon.com/marketplace/latest/buyerguide/buyer-private-offers.html), the subscription and its associated license are tied to a single account. Every other account in your organization needs its own subscription to access that model at the negotiated rate - creating procurement overhead and compliance gaps.

```
Management account ──── ✅ Subscribed (negotiated rate)
Dev account ─────────── ❌ No access
Staging ─────────────── ❌ No access  
Production ──────────── ❌ No access
```

Even when you manually distribute grants via AWS License Manager, those grants land in a **Disabled** state by default. Until explicitly activated, accounts are billed at **public list pricing** - not the rate you negotiated. This is the silent billing blocker.

## Solution

[Managed entitlements for Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/managed-entitlements.html) solves the distribution problem: subscribe once, create a single grant targeting your Organization ID, and every member account inherits access automatically.

This repo has **two independent ways** to create and activate that grant. Pick one:

| | [`lightweight/`](lightweight/) | Full automation (below) |
|---|---|---|
| **What it is** | One script, run once | EventBridge + Lambda + DynamoDB, deployed with AWS CDK or Terraform |
| **Setup** | None — clone and run | Config file + `cdk deploy` or `terraform apply` |
| **Scope** | Bedrock-matching received licenses (no seller allow-list) | Only sellers in your allow-list |
| **Reacts to new offers automatically?** | No — re-run manually | Yes |
| **Choose this if…** | You want the fastest fix, no infra, and trust the Bedrock-matching licenses currently in `ListReceivedLicenses` | You want ongoing, hands-off automation with per-seller control |

→ **Want the lightweight script?** Go straight to [`lightweight/README.md`](lightweight/README.md) — nothing else in this repo is required.

→ **Want the full automation?** Keep reading below.

### Full automation

Deployed with AWS CDK or Terraform, the automation handles grant distribution so you never have to manually create or activate grants again:

1. **Listens** for new Marketplace agreement events via Amazon EventBridge
2. **Verifies** the seller is in your allow-list
3. **Discovers** the license created by the subscription
4. **Creates and activates** an organization-wide grant (addressing the Disabled→Active gotcha)
5. **Retries activation asynchronously** if License Manager is still processing the grant
6. **Notifies** your team via Amazon SNS

See [Architecture](docs/architecture.md#how-it-works) for the full flow.

## Scope & Limitations

- **License Manager-backed products only.** Managed entitlements work through AWS License Manager, which issues licenses for AMI, Container, Machine Learning, and AWS Data Exchange products. Bedrock third-party models are Machine Learning products.
- **No SaaS.** SaaS subscriptions never get a License Manager license, so there is no grant to create or activate.
- **The allow-list matches seller account IDs only.** An allow-listed seller that also sells SaaS triggers a false-positive `License Not Found` alert when you accept its SaaS offer.

See [Scope & Limitations](docs/architecture.md#scope--limitations) for details.

## Quickstart

### Prerequisites

- Your **AWS Organizations management account**, in **`us-east-1`**, with **All features** enabled (not consolidated billing only)
- Python 3.12+, and the AWS CLI with management account credentials
- AWS CDK v2 or Terraform 1.7+, depending on the tool you deploy with
- License Manager and Marketplace organization integration. Check it with `python3 scripts/bootstrap_prereqs.py --check --region us-east-1`. If the check reports `APPLY` items, see [Organization prerequisites](docs/configuration.md#organization-prerequisites).

### Configure

```bash
git clone https://github.com/aws-samples/sample-bedrock-managed-entitlements.git
cd sample-bedrock-managed-entitlements

# Interactive: auto-discovers your org ID and licenses
python3 scripts/setup_config.py

# Or manually:
# cp config/sellers.example.json config/sellers.json && edit config/sellers.json
```

See [Configuration](docs/configuration.md) for every setting, notifications, and Slack.

### Choose CDK or Terraform

Use the tool your team already uses. Both deploy the same functionality with the same default resource names, so the scripts in `scripts/` work with either. The real differences are in state handling, logging, encryption at rest, and a few Terraform-only variables. See [Differences from the CDK stack](docs/terraform.md#differences-from-the-cdk-stack).

### Deploy with AWS CDK

```bash
cd cdk && pip install -r requirements.txt
cdk bootstrap aws://ACCOUNT_ID/us-east-1  # first time only
cdk deploy

# Seed DynamoDB with your allowed sellers
cd .. && python3 scripts/seed_sellers.py --config config/sellers.json
```

Tear down with `cd cdk && cdk destroy`. See [Deploy with AWS CDK](docs/cdk.md) for bootstrapping, configuration, stack outputs, and assertion tests.

### Deploy with Terraform

```bash
cd terraform
terraform init
terraform plan -out=tfplan
terraform apply tfplan

# Seed DynamoDB with your allowed sellers
cd .. && python3 scripts/seed_sellers.py --config config/sellers.json \
  --table-name "$(terraform -chdir=terraform output -raw seller_table_name)"
```

Tear down with `terraform -chdir=terraform destroy`. See [Deploy with Terraform](docs/terraform.md) for variables, state backends, and running next to an existing CDK deployment.

Either teardown deletes the allow-list and pending-grant tables. Back up the allow-list first if you need it.

## Documentation

| Guide | Covers |
|-------|--------|
| [Configuration](docs/configuration.md) | Organization prerequisites, `config/sellers.json`, notifications, finding seller and organization IDs |
| [Architecture](docs/architecture.md) | Scope and limitations, how it works, grant targeting, grant activation |
| [Deploy with AWS CDK](docs/cdk.md) | CDK bootstrapping, configuration, stack outputs, assertion tests, teardown |
| [Deploy with Terraform](docs/terraform.md) | Terraform variables, state, side-by-side with CDK, differences from CDK |
| [Operations](docs/operations.md) | Backfilling existing licenses, legacy grant cleanup, troubleshooting manual activation, cost |
| [Auto-accept offers](docs/auto-accept.md) | Opt-in automatic acceptance of private offers and its risks |
| [Testing and validation](docs/testing.md) | Verifying discounts, testing without live offers, E2E validation, validation status |
| [Security](docs/security.md) | Shared responsibility, IAM permissions, key security considerations |
| [FAQ](docs/faq.md) | Common questions |
| [Scripts](scripts/README.md) | Helper scripts |

## License

MIT-0 - See [LICENSE](LICENSE)
