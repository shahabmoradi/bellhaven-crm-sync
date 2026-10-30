"""Recon: run this FIRST when the CRM token lands, before any
pipeline run. It snapshots every account and prints the field
inventory and cross-checks a human should eyeball (per the build
spec): field spellings and formats, planted history already sitting
in duplicate_of_account / chow_current_account, the notes (which may
carry hints), parent structure, duplicate groups, and the
wrong-parent financial table with the branch each row will take.

    BELLHAVEN_TOKEN=... python3 src/recon.py
"""

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crm
from normalize import normalize_location, account_view, canonical_account
from match import match_location, has_address_anchor
from proposals import resolve_parent, _money

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main():
    raw_accounts = crm.fetch_all_accounts()
    print(f"== ACCOUNTS PULLED: {len(raw_accounts)}")
    snap = DATA / "snapshot_before.json"
    if not snap.exists():
        snap.write_text(json.dumps(raw_accounts, indent=2))
        print(f"== snapshot_before.json saved ({len(raw_accounts)} accounts)")
    accounts = [canonical_account(a) for a in raw_accounts]
    by_id = {a.get("id"): a for a in accounts}

    print("\n== FIELD INVENTORY (key: non-null %, types, samples)")
    keys = sorted({k for a in raw_accounts for k in a.keys()})
    for key in keys:
        vals = [a.get(key) for a in raw_accounts if a.get(key) is not None]
        types = Counter(type(v).__name__ for v in vals)
        samples = [str(v)[:40] for v in vals[:3]]
        print(f"  {key}: {len(vals)}/{len(raw_accounts)} {dict(types)} e.g. {samples}")

    print("\n== STATUS VALUES:", dict(Counter(str(a.get('status')) for a in raw_accounts)))

    print("\n== MONEY FIELDS (formats to eyeball)")
    for key in ("lifetime_revenue", "outstanding_ar"):
        vals = [a.get(key) for a in raw_accounts]
        strings = [v for v in vals if isinstance(v, str)]
        nulls = sum(1 for v in vals if v is None)
        negatives = [v for v in vals if isinstance(v, (int, float)) and v < 0]
        tiny = [v for v in vals if isinstance(v, (int, float)) and 0 < v < 1]
        print(f"  {key}: nulls={nulls} strings={strings[:5]} negatives={negatives[:5]} tiny={tiny[:5]}")

    print("\n== PARENT_ID RESOLUTION")
    bad = [a.get("id") for a in accounts
           if a.get("parent_id") is not None and a.get("parent_id") not in by_id]
    print("  dangling parent_ids on accounts:", bad or "none")
    children = Counter(a.get("parent_id") for a in accounts if a.get("parent_id") is not None)
    for pid, count in children.most_common():
        parent = by_id.get(pid) or {}
        print(f"  parent {pid} ({parent.get('name')}): {count} children")
    print("  Bellhaven-name variants:")
    for a in accounts:
        if re.search(r"bel+h?aven", a.get("name") or "", re.I):
            print(f"    #{a.get('id')} {a.get('name')} parent={a.get('parent_id')} children={children.get(a.get('id'), 0)}")

    print("\n== PRE-EXISTING HISTORY (respect it, don't redo it)")
    for a in accounts:
        if a.get("duplicate_of_account") or a.get("chow_current_account"):
            print(f"  #{a.get('id')} {a.get('name')}: duplicate_of={a.get('duplicate_of_account')} "
                  f"chow_current_account={a.get('chow_current_account')} status={a.get('status')}")

    print("\n== NOTES (read them all — planted hints live here)")
    for a in accounts:
        if a.get("note"):
            print(f"  #{a.get('id')} {a.get('name')}: {str(a['note'])[:200]}")

    parent_id, parent_note = resolve_parent(accounts)
    print(f"\n== CANONICAL PARENT: {parent_id} ({parent_note})")

    locations = json.loads((DATA / "locations.json").read_text())
    views = [account_view(a) for a in accounts]
    print("\n== STATE DISTRIBUTION (CRM):",
          dict(Counter(v['state'] for v in views if v['state'])))
    print("   website: OH 23 / MI 5 / PA 4 / IN 2")

    print("\n== DUPLICATE GROUPS (same normalized street + zip, 2+ accounts)")
    groups = defaultdict(list)
    for v in views:
        if v["house"] and v["zip"]:
            groups[(v["house"], v["street"], v["zip"])].append(v["id"])
    for key, ids in groups.items():
        if len(ids) >= 2:
            print(f"  {key}: accounts {ids}")

    print("\n== ANCHORED PAIRS: phone / administrator agreement (settles the phone weight)")
    phone_agree = phone_total = admin_agree = admin_total = 0
    wrong_parent_rows = []
    amberly_parent = None
    for loc_raw in locations:
        loc = normalize_location(loc_raw)
        result = match_location(loc, views)
        best = result["best"]
        if not best or not best["anchored"]:
            continue
        acct = best["account"]["raw"]
        if loc["phone"] and best["account"]["phone"]:
            phone_total += 1
            phone_agree += loc["phone"] == best["account"]["phone"]
        admin_loc = (loc_raw.get("administrator") or "").strip().lower()
        admin_acct = (acct.get("administrator") or "").strip().lower()
        if admin_loc and admin_acct:
            admin_total += 1
            admin_agree += admin_loc == admin_acct
        if loc_raw["name"] == "Amberly Manor":
            amberly_parent = (acct.get("id"), acct.get("parent_id"))
        if str(acct.get("parent_id")) != str(parent_id):
            rev, ar = _money(acct, "lifetime_revenue"), _money(acct, "outstanding_ar")
            if acct.get("parent_id") is None:
                branch = "assign parent (orphan)"
            elif rev > 0 and ar > 0:
                branch = "CHOW SPLIT"
            else:
                branch = "re-parent directly"
            wrong_parent_rows.append((loc_raw["name"], acct.get("id"), acct.get("parent_id"), rev, ar, branch))
    if phone_total:
        print(f"  phone: {phone_agree}/{phone_total} agree ({phone_agree / phone_total:.0%})")
    if admin_total:
        print(f"  administrator: {admin_agree}/{admin_total} agree ({admin_agree / admin_total:.0%})")
    print(f"  Amberly Manor matched account / its parent: {amberly_parent}")

    print("\n== WRONG-PARENT FINANCIAL TABLE (hand-verify every row)")
    for row in wrong_parent_rows:
        print(f"  {row[0]}: account #{row[1]} parent={row[2]} revenue=${row[3]:,.2f} AR=${row[4]:,.2f} -> {row[5]}")
    if not wrong_parent_rows:
        print("  (none)")


if __name__ == "__main__":
    main()
