#!/usr/bin/env python3
"""Distribute every received AWS License Manager license to your whole AWS
Organization and activate each resulting grant -- with a review step first.

Lightweight alternative to the CDK stack: no config file, no DynamoDB, no
Lambda. Just this script and ambient AWS credentials in the management
account. See lightweight/README.md for when to use this vs. backfill_grants.py
(the allow-list-scoped equivalent) or the full CDK automation.

Default mode is dry-run: it lists every received license and shows exactly
what would be distributed and activated, without calling any mutating API.
Nothing is created until you re-run with --apply --confirm-account-id
<account-id>, which must match the account you're actually running in.

Usage:
    python3 lightweight/distribute_licenses.py

    # Apply after reviewing
    python3 lightweight/distribute_licenses.py --apply --confirm-account-id 123456789012

Unlike backfill_grants.py, this script has no allow-list: it will plan (and,
with --apply, distribute+activate) a grant for every received license that
isn't EXPIRED/DELETED, regardless of issuer. That's what makes it
"lightweight" -- no config/sellers.json to maintain -- but it also means the
dry-run plan is your only review step. Read it before passing --apply.

The script:
    1. Lists every received license (ListReceivedLicenses); skips EXPIRED/DELETED.
    2. Uses the organization ARN (DescribeOrganization) as the grant principal.
    3. --apply only: create_grant -> PENDING_WORKFLOW (already-distributed
       licenses reuse the existing org grant, making re-runs idempotent).
    4. --apply only: polls get_grant until WORKFLOW_COMPLETED (distribution done).
    5. --apply only: create_grant_version Status=ACTIVE
       (ActivationOverrideBehavior=ALL_GRANTS_PERMITTED_BY_ISSUER); does not
       wait for activation to finish.
"""

import argparse
import sys
import time
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError


WORKFLOW_IN_PROGRESS = {"PENDING_WORKFLOW"}
FAILED_STATES = {"REJECTED", "FAILED_WORKFLOW", "DELETED", "PENDING_DELETE"}
IGNORED_LICENSE_STATES = {"EXPIRED", "DELETED"}
DUPLICATE_HINTS = ("already has a grant", "duplicate", "already exist",
                    "already distributed", "conflict")
POLL_INTERVAL = 30
TIMEOUT = 3600


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def license_id_from_arn(arn):
    """Handle both 'license/l-...' and 'license:l-...' ARN forms."""
    return arn.replace("/", ":").rsplit(":", 1)[-1]


def parent_grant_arn(lic):
    """The license records its source grant ARN in LicenseMetadata['grantArn']."""
    for md in lic.get("LicenseMetadata", []):
        if md.get("Name") == "grantArn":
            return md.get("Value")
    return None


def is_duplicate_error(exc):
    code = exc.response.get("Error", {}).get("Code", "")
    message = exc.response.get("Error", {}).get("Message", "")
    blob = ("%s %s" % (code, message)).lower()
    return any(hint in blob for hint in DUPLICATE_HINTS)


def discover_organization_arn(orgs):
    org = orgs.describe_organization()["Organization"]
    log("🏢 organization: %s (%s)" % (org.get("Id"), org["Arn"]))
    return org["Arn"]


def list_received_licenses(lm):
    licenses = []
    token = None
    while True:
        kwargs = {"NextToken": token} if token else {}
        resp = lm.list_received_licenses(**kwargs)
        licenses.extend(resp.get("Licenses", []))
        token = resp.get("NextToken")
        if not token:
            return licenses


def find_distributed_grant(lm, license_arn, principal):
    """Find the existing org grant ARN for this license + grantee principal."""
    filters = [
        {"Name": "LicenseArn", "Values": [license_arn]},
        {"Name": "GranteePrincipalARN", "Values": [principal]},
    ]
    token = None
    while True:
        kwargs = {"Filters": filters}
        if token:
            kwargs["NextToken"] = token
        resp = lm.list_distributed_grants(**kwargs)
        for grant in resp.get("Grants", []):
            return grant["GrantArn"]
        token = resp.get("NextToken")
        if not token:
            return None


def operations_from_parent(parent):
    """Parent grant operations minus CreateGrant."""
    ops = parent.get("AllowedOperations") or parent.get("GrantedOperations") or []
    return [op for op in ops if op != "CreateGrant"]


def plan_licenses(licenses):
    """Split received licenses into (planned, ignored) for the dry-run/plan step."""
    planned = []
    ignored = []
    for lic in licenses:
        status = lic.get("Status")
        if status in IGNORED_LICENSE_STATES:
            ignored.append(lic)
        else:
            planned.append(lic)
    return planned, ignored


def print_plan(planned, ignored, principal, apply_mode):
    print("Mode: %s" % ("apply" if apply_mode else "dry-run"))
    print()
    if planned:
        print("Planned grants (organization-wide, principal: %s):" % principal)
        for lic in planned:
            license_id = license_id_from_arn(lic["LicenseArn"])
            issuer = lic.get("Issuer", {}).get("Name", "Unknown")
            product = lic.get("ProductName", "Unknown")
            print("- License: %s" % license_id)
            print("  Issuer : %s" % issuer)
            print("  Product: %s" % product)
    else:
        print("No grants planned.")

    if ignored:
        print()
        print("Ignored (EXPIRED/DELETED):")
        for lic in ignored:
            print("- %s" % license_id_from_arn(lic["LicenseArn"]))

    if not apply_mode:
        print()
        print("Dry-run only -- no API calls that create or modify grants were made.")
        print("This has NO allow-list: every license above would be distributed and")
        print("activated org-wide with --apply, regardless of issuer.")
        print("Re-run with --apply --confirm-account-id <account-id> to proceed.")


def create_grant(lm, lic, principal, operations, name):
    """Returns the grant ARN, or None if the license was already distributed."""
    arn = lic["LicenseArn"]
    home_region = lic.get("HomeRegion") or lm.meta.region_name

    log("📦 Distributing license to the organization")
    log("   grant name : %s" % name)
    log("   license    : %s" % arn)
    log("   home region: %s" % home_region)
    log("   principal  : %s" % principal)
    log("   operations : %s" % ", ".join(operations))

    try:
        resp = lm.create_grant(
            ClientToken=str(uuid.uuid4()),
            GrantName=name,
            LicenseArn=arn,
            Principals=[principal],
            HomeRegion=home_region,
            AllowedOperations=operations,
        )
    except ClientError as exc:
        if is_duplicate_error(exc):
            existing = find_distributed_grant(lm, arn, principal)
            if existing:
                log("   ↩️ already distributed; using existing grant %s" % existing)
                return existing
            log("   ↩️ already distributed but existing grant not found; skipping")
            return None
        raise
    log("   ✅ grant created: %s (version %s)" % (resp["GrantArn"], resp.get("Version")))
    return resp["GrantArn"]


def wait_for_workflow(lm, grant_arn, what):
    log("   ⏳ waiting for %s to complete (timeout %ds)..." % (what, TIMEOUT))
    start = time.monotonic()
    deadline = start + TIMEOUT
    last_status = None
    while True:
        grant = lm.get_grant(GrantArn=grant_arn)["Grant"]
        status = grant.get("GrantStatus")
        if status != last_status:
            log("   status: %s" % status)
        last_status = status
        if status in FAILED_STATES:
            raise RuntimeError("%s failed (status=%s): %s"
                                % (what, status, grant.get("StatusReason") or "no reason"))
        if status not in WORKFLOW_IN_PROGRESS:
            log("   ✅ %s complete (status=%s)" % (what, status))
            return grant
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out after %ds; still %s" % (TIMEOUT, status))
        log("   💓 still %s (%ds elapsed); next check in %ds"
            % (status, int(time.monotonic() - start), POLL_INTERVAL))
        time.sleep(POLL_INTERVAL)


def activate_grant(lm, grant):
    """Returns True if an activation was issued, False if already ACTIVE."""
    if grant.get("GrantStatus") == "ACTIVE":
        log("   ℹ️ grant already ACTIVE; nothing to do")
        return False
    log("   🚀 activating grant...")
    resp = lm.create_grant_version(
        ClientToken=str(uuid.uuid4()),
        GrantArn=grant["GrantArn"],
        Status="ACTIVE",
        SourceVersion=grant.get("Version"),
        Options={"ActivationOverrideBehavior": "ALL_GRANTS_PERMITTED_BY_ISSUER"},
    )
    log("   🎉 activation submitted (version %s)" % resp.get("Version"))
    return True


def process(lm, lic, principal):
    """Returns 'done', 'skipped', or 'failed'. Caller has already filtered ignored licenses."""
    arn = lic["LicenseArn"]
    license_id = license_id_from_arn(arn)
    log("─" * 70)
    log("🔎 license: %s" % license_id)

    grant_arn = parent_grant_arn(lic)
    if not grant_arn:
        log("   ✖ no parent grant ARN in license metadata; cannot distribute.")
        return "failed"

    try:
        parent = lm.get_grant(GrantArn=grant_arn)["Grant"]
        operations = operations_from_parent(parent)
        if not operations:
            log("   ✖ parent grant has no distributable operations (only CreateGrant?).")
            return "failed"
        name = "Grant to my organization"

        new_grant_arn = create_grant(lm, lic, principal, operations, name)
        if new_grant_arn is None:
            return "skipped"
        grant = wait_for_workflow(lm, new_grant_arn, "distribution")
        activate_grant(lm, grant)
        log("   ✔ done: %s" % license_id)
        return "done"
    except (ClientError, BotoCoreError, RuntimeError) as exc:
        log("   ✖ failed for '%s': %s" % (license_id, exc))
        return "failed"


def validate_apply_context(sts, confirm_account_id):
    caller_account = sts.get_caller_identity()["Account"]
    if confirm_account_id != caller_account:
        print(
            "Apply mode requires --confirm-account-id to match the current "
            "AWS account (%s)." % caller_account
        )
        return 1
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Distribute and activate every received License Manager license "
            "org-wide. Defaults to dry-run; requires --apply "
            "--confirm-account-id to make changes."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the plan: create and activate grants. Without this flag, "
             "only prints what would happen.",
    )
    parser.add_argument(
        "--confirm-account-id",
        default=None,
        help="Required with --apply. Must match the current AWS account ID.",
    )
    return parser


def main():
    args = build_parser().parse_args()

    lm = boto3.client("license-manager")
    orgs = boto3.client("organizations")
    try:
        principal = discover_organization_arn(orgs)
        licenses = list_received_licenses(lm)
    except (ClientError, BotoCoreError) as exc:
        log("setup failed: %s" % exc)
        return 2

    if not licenses:
        log("no received licenses found in this region; nothing to do.")
        return 0

    planned, ignored = plan_licenses(licenses)
    print_plan(planned, ignored, principal, args.apply)

    if not args.apply:
        return 0

    if not planned:
        return 0

    sts = boto3.client("sts")
    try:
        validation = validate_apply_context(sts, args.confirm_account_id)
    except (ClientError, BotoCoreError) as exc:
        log("apply validation failed: %s" % exc)
        return 1
    if validation:
        return validation

    print()
    log("found %d received license(s) to process" % len(planned))

    results = [process(lm, lic, principal) for lic in planned]
    done = results.count("done")
    skipped = results.count("skipped")
    failed = results.count("failed")

    log("═" * 70)
    log("📊 Summary: %d distributed+activated, %d already distributed, "
        "%d ignored (expired/deleted), %d failed, %d total"
        % (done, skipped, len(ignored), failed, len(licenses)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
