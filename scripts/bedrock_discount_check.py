#!/usr/bin/env python3
"""
bedrock_discount_check.py

Confirms a negotiated Bedrock Marketplace Private Offer (MPPO) discount is
actually applying in THIS account. Run it in the consuming (member) account
that makes the Bedrock API calls - e.g. paste it into AWS CloudShell in that
account. It uses your ambient credentials; no setup, no cross-account roles.

It replicates the manual SOP:

  Step 1  Grant status   In License Manager (us-east-1) -> Granted Licenses,
                         every grant should be ACTIVE, not DISABLED.
                         "All Features" grants auto-accept but sit at DISABLED
                         until explicitly activated - that's the silent blocker.
                         API: license-manager ListReceivedLicenses / ListReceivedGrants

  Step 2  The fix        For anything not ACTIVE, tell you what action clears it
                         (this tool is read-only; it diagnoses, it doesn't change
                         anything).

  Step 3  Billing proof  Compare the effective per-unit rate (UnblendedCost /
                         UsageQuantity) on AWS Marketplace Bedrock usage BEFORE vs
                         AFTER the offer activation date. Shows old rate, new rate,
                         and the effective discount % per usage type so you can
                         compare it to the rate you negotiated.
                         API: cost-explorer GetCostAndUsage

API shapes verified against boto3 / AWS docs (2026-06). GrantStatus enum includes
ACTIVE and DISABLED; ReceivedStatus mirrors it.

Examples
--------
  # Grants + billing proof, run inside the member account:
  python bedrock_discount_check.py --activation-date 2026-06-11

  # Only Anthropic grants, flag specific models you expect to be present:
  python bedrock_discount_check.py --issuer "Anthropic, PBC" \\
      --expect "Claude Opus 4.8,Claude Sonnet 4.6"

  # Grants only, skip billing:
  python bedrock_discount_check.py --no-billing

  # Run from the payer instead, scope billing to one member account:
  python bedrock_discount_check.py --activation-date 2026-06-01 --linked-account 111122223333
"""

import argparse
import datetime as dt
import json
import sys

try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError
except ImportError:
    sys.exit("boto3 is required. In CloudShell it's preinstalled; otherwise: pip install boto3")


def _as_date(v):
    """Coerce a License Manager date (datetime or ISO string) to YYYY-MM-DD."""
    if not v:
        return None
    if hasattr(v, "date"):
        try:
            return v.date().isoformat()
        except Exception:
            return None
    s = str(v)
    return s[:10] if len(s) >= 10 else None


class C:
    _on = sys.stdout.isatty()
    GREEN = "\033[32m" if _on else ""
    RED = "\033[31m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    GREY = "\033[90m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    RESET = "\033[0m" if _on else ""


# Map each non-ACTIVE grant state to a plain-English diagnosis + the fix.
GRANT_STATE_FIX = {
    "ACTIVE": ("ok", "Grant is active. Discount can flow (confirm with the billing check)."),
    "DISABLED": ("blocked",
                 "Grant is accepted but DISABLED. Activate it: grantor selects the parent grant in "
                 "License Manager -> Activate (bulk), or this account activates it in License Manager "
                 "(us-east-1) -> Granted Licenses -> Activate. The negotiated rate does NOT apply until "
                 "this is done."),
    "PENDING_ACCEPT": ("blocked",
                       "Grant distributed but not accepted. Accept it in License Manager -> Granted "
                       "Licenses, then activate."),
    "PENDING_WORKFLOW": ("pending", "Grant is still provisioning. Re-check shortly."),
    "WORKFLOW_COMPLETED": ("pending", "Workflow completed; should transition to ACTIVE. Re-check shortly."),
    "REJECTED": ("blocked", "Grant was REJECTED. The grantor must re-issue it."),
    "FAILED_WORKFLOW": ("blocked", "Grant provisioning FAILED. Grantor must re-issue."),
    "DELETED": ("blocked", "Grant DELETED. Grantor must re-issue."),
    "PENDING_DELETE": ("blocked", "Grant pending delete. Grantor must re-issue."),
}


# ---------------------------------------------------------------------------
# Step 1: grant status in this account
# ---------------------------------------------------------------------------
def list_received_licenses(lm, issuer):
    filters = [{"Name": "IssuerName", "Values": [issuer]}] if issuer else []
    out, token = [], None
    while True:
        kw = {"MaxResults": 100}
        if filters:
            kw["Filters"] = filters
        if token:
            kw["NextToken"] = token
        resp = lm.list_received_licenses(**kw)
        out.extend(resp.get("Licenses", []))
        token = resp.get("NextToken")
        if not token:
            break
    return out


def list_grants_for_license(lm, license_arn):
    out, token = [], None
    while True:
        kw = {"Filters": [{"Name": "LicenseArn", "Values": [license_arn]}], "MaxResults": 100}
        if token:
            kw["NextToken"] = token
        resp = lm.list_received_grants(**kw)
        out.extend(resp.get("Grants", []))
        token = resp.get("NextToken")
        if not token:
            break
    return out


def check_grants(region, issuer, expect):
    lm = boto3.client("license-manager", region_name=region)
    licenses = list_received_licenses(lm, issuer)

    rows = []
    seen_products = set()
    for lic in licenses:
        product = lic.get("ProductName") or lic.get("LicenseName") or "(unnamed)"
        seen_products.add(product)
        arn = lic.get("LicenseArn")
        recv = (lic.get("ReceivedMetadata") or {}).get("ReceivedStatus")
        grants = list_grants_for_license(lm, arn) if arn else []
        # The grant state is what gates the discount. If multiple grants, prefer
        # ACTIVE; else surface the most relevant non-active one.
        statuses = [g.get("GrantStatus") for g in grants]
        if "ACTIVE" in statuses:
            effective = "ACTIVE"
        elif statuses:
            # pick the first non-active by a rough severity order
            order = ["DISABLED", "PENDING_ACCEPT", "PENDING_WORKFLOW", "WORKFLOW_COMPLETED",
                     "REJECTED", "FAILED_WORKFLOW", "PENDING_DELETE", "DELETED"]
            effective = next((s for s in order if s in statuses), statuses[0])
        else:
            # No grant record but license received: fall back to the license's received status
            effective = recv or "NO_GRANT"
        rows.append({
            "product": product,
            "license_arn": arn,
            "received_status": recv,
            "grant_status": effective,
            "grant_count": len(grants),
            "validity_begin": _as_date((lic.get("Validity") or {}).get("Begin")),
            "create_time": _as_date(lic.get("CreateTime")),
        })

    # Flag expected-but-absent models (never distributed to this account = Canva case)
    missing = []
    for want in expect:
        if not any(want.lower() in (r["product"] or "").lower() for r in rows):
            missing.append(want)

    return rows, missing


def print_grants(rows, missing, account_id, region):
    print(f"\n{C.BOLD}Step 1 - Grant status  (account {account_id}, License Manager {region}){C.RESET}")
    if not rows:
        print(f"  {C.YELLOW}No received licenses found"
              f"{' for that issuer' if True else ''}. "
              f"Either nothing distributed to this account, or wrong issuer/region.{C.RESET}")
    for r in sorted(rows, key=lambda x: x["product"]):
        gs = r["grant_status"]
        kind, fix = GRANT_STATE_FIX.get(gs, ("blocked", f"Unrecognised grant status: {gs}."))
        tag = {"ok": f"{C.GREEN}ACTIVE{C.RESET}",
               "pending": f"{C.YELLOW}{gs}{C.RESET}",
               "blocked": f"{C.RED}{gs}{C.RESET}"}.get(kind, gs)
        print(f"  {tag:24} {C.BOLD}{r['product']}{C.RESET}")
        if kind != "ok":
            print(f"      {C.GREY}{r['license_arn']}{C.RESET}")
            print(f"      {C.YELLOW}fix:{C.RESET} {fix}")
    if missing:
        print(f"\n  {C.RED}Expected but NOT present in this account "
              f"(grant never distributed here):{C.RESET}")
        for m in missing:
            print(f"    - {m}  -> grantor must distribute the grant to this account first.")
    return rows, missing


# ---------------------------------------------------------------------------
# Step 3: billing rate proof (pre vs post activation)
# ---------------------------------------------------------------------------
def ce_rates(ce, start, end, linked_account, service):
    """Return {usage_type: (cost, qty)} summed over [start, end)."""
    and_filters = [{"Dimensions": {"Key": "BILLING_ENTITY", "Values": ["AWS Marketplace"]}}]
    if linked_account:
        and_filters.append({"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [linked_account]}})
    if service:
        and_filters.append({"Dimensions": {"Key": "SERVICE", "Values": [service]}})
    cost_filter = and_filters[0] if len(and_filters) == 1 else {"And": and_filters}

    agg = {}
    token = None
    while True:
        kw = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost", "UsageQuantity"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            "Filter": cost_filter,
        }
        if token:
            kw["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kw)
        for period in resp.get("ResultsByTime", []):
            for grp in period.get("Groups", []):
                ut = grp["Keys"][0]
                cost = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                qty = float(grp["Metrics"]["UsageQuantity"]["Amount"])
                c, q = agg.get(ut, (0.0, 0.0))
                agg[ut] = (c + cost, q + qty)
        token = resp.get("NextPageToken")
        if not token:
            break
    return agg


def billing_proof(activation_date, window_days, linked_account, service):
    ce = boto3.client("ce", region_name="us-east-1")
    act = dt.date.fromisoformat(activation_date)
    today = dt.date.today()

    pre_start = act - dt.timedelta(days=window_days)
    pre_end = act                                   # exclusive -> up to day before activation
    post_start = act + dt.timedelta(days=1)         # skip the partial activation day
    post_end = min(today, post_start + dt.timedelta(days=window_days))  # exclusive, excludes today

    if post_end <= post_start:
        return None, ("Not enough post-activation data yet (need at least 1 full day after the "
                      f"activation date {activation_date}). Re-run once a few days have passed.")

    pre = ce_rates(ce, pre_start.isoformat(), pre_end.isoformat(), linked_account, service)
    post = ce_rates(ce, post_start.isoformat(), post_end.isoformat(), linked_account, service)

    rows = []
    for ut in sorted(set(pre) | set(post)):
        pc, pq = pre.get(ut, (0.0, 0.0))
        oc, oq = post.get(ut, (0.0, 0.0))
        pre_rate = pc / pq if pq else None
        post_rate = oc / oq if oq else None
        if pre_rate and post_rate:
            disc = (pre_rate - post_rate) / pre_rate * 100.0
        else:
            disc = None
        rows.append({
            "usage_type": ut,
            "pre_rate": pre_rate,
            "post_rate": post_rate,
            "discount_pct": disc,
            "post_spend": oc,
        })
    meta = {
        "pre_window": f"{pre_start} .. {pre_end} (excl)",
        "post_window": f"{post_start} .. {post_end} (excl)",
    }
    return (rows, meta), None


def print_billing(result, err, activation_date):
    print(f"\n{C.BOLD}Step 3 - Billing rate proof  (activation {activation_date}){C.RESET}")
    if err:
        print(f"  {C.YELLOW}{err}{C.RESET}")
        return
    rows, meta = result
    print(f"  {C.GREY}pre:  {meta['pre_window']}{C.RESET}")
    print(f"  {C.GREY}post: {meta['post_window']}{C.RESET}")
    print(f"  {C.GREY}rate = UnblendedCost / UsageQuantity (per CE usage unit); "
          f"discount % = rate reduction.{C.RESET}")
    material = [r for r in rows if r["post_spend"] and r["post_spend"] > 0]
    if not material:
        print(f"  {C.YELLOW}No AWS Marketplace Bedrock usage in the post window.{C.RESET}")
        return
    print(f"\n  {'USAGE TYPE':32} {'OLD $/unit':>12} {'NEW $/unit':>12} {'DISCOUNT':>10}")
    for r in sorted(material, key=lambda x: -x["post_spend"]):
        old = f"{r['pre_rate']:.4f}" if r["pre_rate"] is not None else "n/a"
        new = f"{r['post_rate']:.4f}" if r["post_rate"] is not None else "n/a"
        if r["discount_pct"] is None:
            disc = "n/a"
            colour = C.GREY
        elif r["discount_pct"] >= 1.0:
            disc = f"{r['discount_pct']:.1f}%"
            colour = C.GREEN
        else:
            disc = f"{r['discount_pct']:.1f}%"
            colour = C.RED
        print(f"  {r['usage_type'][:32]:32} {old:>12} {new:>12} {colour}{disc:>10}{C.RESET}")
    print(f"\n  {C.GREY}Compare the discount % above to the rate you negotiated in the MPPO. "
          f"~0% means the discount is NOT flowing despite any active grant.{C.RESET}")


# ---------------------------------------------------------------------------
def derive_activation_date(rows):
    """Best-guess the offer activation pivot from received-license validity.
    Returns (earliest_date, [all_distinct_dates]). Prefers Validity.Begin,
    falls back to CreateTime."""
    dates = []
    for r in rows:
        d = r.get("validity_begin") or r.get("create_time")
        if d:
            dates.append(d)
    if not dates:
        return None, []
    distinct = sorted(set(dates))
    return distinct[0], distinct


def whoami(region):
    try:
        return boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    except Exception:
        return "unknown"


def main():
    ap = argparse.ArgumentParser(
        description="Verify a Bedrock MPPO discount is applying in THIS account (grants + billing).")
    ap.add_argument("--activation-date", help="Offer activation date YYYY-MM-DD. Optional - if omitted, "
                                              "auto-derived from license validity. Pass to override.")
    ap.add_argument("--window-days", type=int, default=10, help="Days each side of activation (default 10)")
    ap.add_argument("--issuer", help='Filter grants by license issuer, e.g. "Anthropic, PBC"')
    ap.add_argument("--expect", default="", help='Comma-separated product names you expect present, '
                                                 'e.g. "Claude Opus 4.8,Claude Sonnet 4.6"')
    ap.add_argument("--region", default="us-east-1", help="License Manager region (default us-east-1)")
    ap.add_argument("--linked-account", help="If run from the payer, scope billing to this member account ID")
    ap.add_argument("--service", help='Optional CE SERVICE filter, e.g. "Claude (Amazon Bedrock Edition)"')
    ap.add_argument("--no-billing", action="store_true", help="Run grant check only (skip Step 3)")
    ap.add_argument("--json", metavar="PATH", help="Write results as JSON")
    args = ap.parse_args()

    account = whoami(args.region)
    expect = [e.strip() for e in args.expect.split(",") if e.strip()]
    print(f"{C.GREY}Running as account {account}{C.RESET}")

    payload = {"account": account}

    try:
        rows, missing = check_grants(args.region, args.issuer, expect)
        print_grants(rows, missing, account, args.region)
        payload["grants"] = rows
        payload["missing"] = missing
    except (ClientError, BotoCoreError) as e:
        msg = str(e)
        print(f"\n{C.BOLD}Step 1 - Grant status  (account {account}, License Manager {args.region}){C.RESET}")
        if "Service role not found" in msg or "create the required role" in msg:
            print(f"  {C.YELLOW}License Manager isn't initialised in this account.{C.RESET} "
                  f"Open the License Manager console once in {args.region} to complete first-time "
                  f"setup (it creates the required service role), then re-run.")
            print(f"  {C.GREY}Note: an account that has actually received marketplace grants will "
                  f"already have this set up, so this usually only appears in accounts that have "
                  f"never used License Manager.{C.RESET}")
        elif "AccessDenied" in msg or "not authorized" in msg:
            print(f"  {C.YELLOW}Access denied.{C.RESET} This identity needs "
                  f"license-manager:ListReceivedLicenses and ListReceivedGrants.")
        else:
            print(f"  {C.RED}Grant check failed:{C.RESET} {msg}")
        payload["grants_error"] = msg

    if not args.no_billing:
        act_date = args.activation_date
        derived = False
        distinct = []
        if not act_date:
            act_date, distinct = derive_activation_date(payload.get("grants", []))
            derived = act_date is not None
        if not act_date:
            print(f"\n{C.YELLOW}Step 3 skipped: couldn't determine an activation date.{C.RESET} "
                  f"No received licenses to derive it from. Pass --activation-date YYYY-MM-DD to run "
                  f"the billing proof.")
        else:
            if derived:
                note = f"Activation date {act_date} auto-derived from license validity"
                if len(distinct) > 1:
                    note += (f" (licenses span {distinct[0]}..{distinct[-1]}; using earliest. "
                             f"Pass --activation-date to override)")
                print(f"\n{C.GREY}{note}.{C.RESET}")
            try:
                result, err = billing_proof(act_date, args.window_days,
                                            args.linked_account, args.service)
                print_billing(result, err, act_date)
                if result:
                    payload["billing"] = {"rows": result[0], "windows": result[1],
                                          "activation_date": act_date, "derived": derived}
                else:
                    payload["billing_note"] = err
            except (ClientError, BotoCoreError) as e:
                msg = str(e)
                print(f"\n  {C.RED}Billing check failed:{C.RESET} {msg}")
                if "AccessDenied" in msg or "not authorized" in msg:
                    print(f"  {C.YELLOW}Cost Explorer may not be enabled for this member account. "
                          f"Run from an account with CE access and pass --linked-account "
                          f"{account}.{C.RESET}")
                payload["billing_error"] = msg

    if args.json:
        with open(args.json, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        print(f"\n{C.GREY}Wrote {args.json}{C.RESET}")


if __name__ == "__main__":
    main()
