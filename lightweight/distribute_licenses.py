#!/usr/bin/env python3
"""Distribute received Bedrock AWS License Manager licenses to your whole AWS
Organization and activate each resulting grant -- with a review step first.

Lightweight alternative to the CDK stack: no config file, no DynamoDB, no
Lambda. Just this script and ambient AWS credentials in the management
account. See lightweight/README.md for when to use this vs. backfill_grants.py
(the allow-list-scoped equivalent) or the full CDK automation.

Default mode is dry-run: it lists every received Bedrock-matching license and
shows exactly what would be distributed and activated, without calling any
mutating API. Nothing is created until you re-run with --apply --confirm-account-id
<account-id>, which must match the account you're actually running in.

Usage:
    python3 lightweight/distribute_licenses.py

    # Apply after reviewing
    python3 lightweight/distribute_licenses.py --apply --confirm-account-id 123456789012

Unlike backfill_grants.py, this script has no seller allow-list. It scopes by
license name instead: only received licenses whose LicenseName or ProductName
contains "bedrock" are planned. That's what makes it "lightweight" -- no
config/sellers.json to maintain -- but it also means the dry-run plan is your
only review step. Read it before passing --apply.

The script:
    1. Lists received licenses in us-east-1 (ListReceivedLicenses); skips
       EXPIRED/DELETED and non-Bedrock licenses.
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
LICENSE_MANAGER_REGION = "us-east-1"
LICENSE_NAME_FILTER = "bedrock"
POLL_INTERVAL = 30
TIMEOUT = 3600

# create_grant/create_grant_version return a retriable error when License Manager's
# cap on concurrent org grant activities is reached; with_org_retry waits and retries.
ORG_ACTIVITY_IN_PROGRESS_HINT = "too many concurrent org grants"
ORG_RETRY_MAX_WAIT = 1800
ORG_RETRY_BACKOFF_CAP = 300


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


def license_name(lic):
    """Human-readable name for logging: LicenseName, else ProductName, else '?'."""
    return lic.get("LicenseName") or lic.get("ProductName") or "?"


def matches_license_scope(lic):
    """True if LicenseName or ProductName contains LICENSE_NAME_FILTER."""
    names = (lic.get("LicenseName") or "", lic.get("ProductName") or "")
    return any(LICENSE_NAME_FILTER in name.lower() for name in names)


def is_duplicate_error(exc):
    code = exc.response.get("Error", {}).get("Code", "")
    message = exc.response.get("Error", {}).get("Message", "")
    blob = ("%s %s" % (code, message)).lower()
    return any(hint in blob for hint in DUPLICATE_HINTS)


def is_org_activity_in_progress(exc):
    """True when the call hit License Manager's cap on concurrent org grant activities."""
    if not isinstance(exc, ClientError):
        return False
    err = exc.response.get("Error", {})
    blob = ("%s %s" % (err.get("Code", ""), err.get("Message", ""))).lower()
    return ORG_ACTIVITY_IN_PROGRESS_HINT in blob


def with_org_retry(fn, what):
    """Run fn(), retrying while the org grant activity cap is reached."""
    delay = POLL_INTERVAL
    waited = 0
    while True:
        try:
            return fn()
        except ClientError as exc:
            if not (is_org_activity_in_progress(exc) and waited < ORG_RETRY_MAX_WAIT):
                raise
            log("   ⏳ organization grant activity at capacity; retrying %s in %ds (%ds elapsed)"
                % (what, delay, waited))
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, ORG_RETRY_BACKOFF_CAP)


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
    """Split received licenses into (planned, ignored, excluded) for dry-run."""
    planned = []
    ignored = []
    excluded = []
    for lic in licenses:
        status = lic.get("Status")
        if status in IGNORED_LICENSE_STATES:
            ignored.append(lic)
        elif matches_license_scope(lic):
            planned.append(lic)
        else:
            excluded.append(lic)
    return planned, ignored, excluded


def print_plan(planned, ignored, excluded, principal, apply_mode):
    print("Mode: %s" % ("apply" if apply_mode else "dry-run"))
    print("License Manager region: %s" % LICENSE_MANAGER_REGION)
    print("License scope filter: %r in LicenseName or ProductName" % LICENSE_NAME_FILTER)
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

    if excluded:
        print()
        print("Excluded by scope filter: %d" % len(excluded))

    if not apply_mode:
        print()
        print("Dry-run only -- no API calls that create or modify grants were made.")
        print("This has NO seller allow-list: every Bedrock-matching license above")
        print("would be distributed and activated org-wide with --apply.")
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
        resp = with_org_retry(lambda: lm.create_grant(
            ClientToken=str(uuid.uuid4()),
            GrantName=name,
            LicenseArn=arn,
            Principals=[principal],
            HomeRegion=home_region,
            AllowedOperations=operations,
        ), "distribution")
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
    resp = with_org_retry(lambda: lm.create_grant_version(
        ClientToken=str(uuid.uuid4()),
        GrantArn=grant["GrantArn"],
        Status="ACTIVE",
        SourceVersion=grant.get("Version"),
        Options={"ActivationOverrideBehavior": "ALL_GRANTS_PERMITTED_BY_ISSUER"},
    ), "activation")
    log("   🎉 activation submitted (version %s)" % resp.get("Version"))
    return True


def process(lm, lic, principal):
    """Returns 'done', 'skipped', or 'failed'. Caller has already filtered ignored licenses."""
    arn = lic["LicenseArn"]
    license_id = license_id_from_arn(arn)
    log("─" * 70)
    log("🔎 license: %s (%s)" % (license_id, license_name(lic)))

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
            "Distribute and activate received Bedrock License Manager licenses "
            "org-wide from us-east-1. Defaults to dry-run; requires --apply "
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
    started = time.time()

    lm = boto3.client("license-manager", region_name=LICENSE_MANAGER_REGION)
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

    planned, ignored, excluded = plan_licenses(licenses)
    print_plan(planned, ignored, excluded, principal, args.apply)

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
    log("found %d Bedrock-matching received license(s) to process" % len(planned))

    results = [process(lm, lic, principal) for lic in planned]
    done = results.count("done")
    skipped = results.count("skipped")
    failed = results.count("failed")

    log("═" * 70)
    log("📊 Summary: %d distributed+activated, %d already distributed, "
        "%d ignored (expired/deleted), %d excluded by scope, %d failed, %d total"
        % (done, skipped, len(ignored), len(excluded), failed, len(licenses)))
    log("   elapsed: %.1fs" % (time.time() - started))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
