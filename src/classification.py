"""Classification report: every website location, its category, and
its CRM account(s) — plus every CRM account that is not on the
website and what happened to it.

    python3 src/classification.py [--fixture ACCOUNTS_JSON]

Writes classification.csv at the repo root. Categories are the four
from the exercise — confident_match, match_needing_fix,
no_crm_account, crm_account_not_on_website — with a detail column
carrying the mechanism (rename, duplicate cluster, CHOW split,
confirmed non-match -> create, preserved CHOW original, ...).

The report is derived, never hand-typed: locations from
data/locations.json, accounts from the CRM (or a fixture export),
classifications from the same proposal builder the pipeline runs,
and decision state from the proposal store. Re-running it after the
review session produces the final version, with created accounts
carrying their real ids.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crm
import store
from normalize import canonical_account
from proposals import build_proposals

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

CATEGORY = {
    "confident": "confident_match",
    "fix": "match_needing_fix",
    "duplicate": "match_needing_fix",
    "chow_split": "match_needing_fix",
    "create": "no_crm_account",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", help="local accounts JSON (e.g. a saved export)")
    args = ap.parse_args()

    locations = json.loads((DATA / "locations.json").read_text())
    if args.fixture:
        raw = json.loads(Path(args.fixture).read_text())
        accounts_raw = raw["accounts"] if isinstance(raw, dict) else raw
    else:
        accounts_raw = crm.fetch_all_accounts()
    accounts = [canonical_account(a) for a in accounts_raw]
    by_id = {str(a["id"]): a for a in accounts}

    conn = store.connect()
    non_matches = store.confirmed_non_matches(conn)
    import time
    proposals, summary = build_proposals(locations, accounts,
                                         time.strftime("%Y-%m-%d"),
                                         non_matches=non_matches)
    decided = {}
    for p in store.list_proposals(conn):
        decided.setdefault(p["subject_key"], []).append(p)

    rows = []
    covered_accounts = set()

    def decision_note(subject):
        notes = []
        for p in decided.get(subject, []):
            if p["status"] in ("applied", "rejected"):
                notes.append(f"{p['kind']} {p['status']}"
                             + (f" ({p['reason']})" if p.get("reason") else ""))
        return "; ".join(notes)

    prop_by_subject = {p["subject_key"]: p for p in proposals}
    for loc in locations:
        slug = loc.get("slug") or ""
        prop = prop_by_subject.get(slug)
        if prop is None:
            conf = next((c for c in summary["confident_no_change"]
                         if c["location"] == loc["name"]), None)
            acct_id = str(conf["account_id"]) if conf else ""
            covered_accounts.add(acct_id)
            detail = "matched; no change needed"
            # A location whose account exists because of an applied
            # split is a resolved fix, not an untouched match.
            prior = [d for d in decided.get(slug, []) if d["status"] == "applied"]
            if prior:
                detail = "resolved: " + "; ".join(d["kind"] for d in prior)
            rows.append({
                "location_name": loc["name"], "slug": slug,
                "website_street": loc.get("street_address"),
                "website_city": loc.get("city"), "website_state": loc.get("state"),
                "website_zip": loc.get("zip"),
                "category": "confident_match" if not prior else "match_needing_fix",
                "detail": detail,
                "crm_account_ids": acct_id,
                "crm_account_names": by_id.get(acct_id, {}).get("name", ""),
                "decision": decision_note(slug),
            })
            continue
        ev = prop["evidence"]
        ids = [str(i) for i in ev.get("cluster_account_ids", [])]
        if not ids and ev.get("account"):
            ids = [str(ev["account"]["id"])]
        covered_accounts.update(ids)
        detail_bits = []
        if prop["kind"] == "duplicate":
            detail_bits.append("duplicate cluster; survivor kept, losers "
                               "duplicate_of_account + Inactive (CHOW link "
                               "only if a loser carries revenue and AR)")
        if prop["kind"] == "chow_split":
            detail_bits.append("CHOW split: old account preserved, new "
                               "account created under Bellhaven parent")
        if prop["kind"] == "create" and (slug, ) and any(
                s == slug for s, _ in non_matches):
            detail_bits.append("confirmed non-match on review; the "
                               "look-alike account belongs to another "
                               "operator and is left untouched")
        for kind_note in ev.get("notes", []):
            detail_bits.append(kind_note)
        for f, old, new in ev.get("diff", []):
            detail_bits.append(f"{f}: {old!r} -> {new!r}")
        rows.append({
            "location_name": loc["name"], "slug": slug,
            "website_street": loc.get("street_address"),
            "website_city": loc.get("city"), "website_state": loc.get("state"),
            "website_zip": loc.get("zip"),
            "category": CATEGORY[prop["kind"]],
            "detail": " | ".join(detail_bits) or prop["kind"],
            "crm_account_ids": ";".join(ids),
            "crm_account_names": "; ".join(by_id.get(i, {}).get("name", "") for i in ids),
            "decision": decision_note(slug) or f"{prop['kind']} pending",
        })

    # Accounts not on the website: Bellhaven-parent children and
    # Bellhaven-named orphans the matcher did not claim, plus every
    # preserved CHOW original (historical, deliberately untouched).
    parent_id = summary["parent_id"]
    for acct in accounts:
        aid = str(acct["id"])
        if aid in covered_accounts or aid == str(parent_id):
            continue
        is_child = str(acct.get("parent_id")) == str(parent_id)
        is_orphan = acct.get("parent_id") is None and "bellhaven" in (acct.get("name") or "").lower()
        if acct.get("chow_current_account"):
            rows.append({
                "location_name": "", "slug": "",
                "website_street": "", "website_city": "",
                "website_state": "", "website_zip": "",
                "category": "crm_account_not_on_website",
                "detail": ("preserved CHOW original: facility's live "
                           "account is its chow_current_account target; "
                           "this record is kept untouched for billing "
                           "history"),
                "crm_account_ids": aid,
                "crm_account_names": acct.get("name", ""),
                "decision": "none (by design)",
            })
        elif is_child or is_orphan:
            subject = f"account:{acct['id']}"
            rows.append({
                "location_name": "", "slug": "",
                "website_street": "", "website_city": "",
                "website_state": "", "website_zip": "",
                "category": "crm_account_not_on_website",
                "detail": "under the Bellhaven parent (or Bellhaven-named "
                          "orphan) but no website location matches it",
                "crm_account_ids": aid,
                "crm_account_names": acct.get("name", ""),
                "decision": decision_note(subject) or "inactivate pending",
            })

    out = ROOT / "classification.csv"
    fields = ["location_name", "slug", "website_street", "website_city",
              "website_state", "website_zip", "category", "detail",
              "crm_account_ids", "crm_account_names", "decision"]
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    loc_rows = sum(1 for r in rows if r["location_name"])
    print(f"wrote {out}: {len(rows)} rows ({loc_rows} locations, "
          f"{len(rows) - loc_rows} not-on-website accounts)")
    from collections import Counter
    print(Counter(r["category"] for r in rows))


if __name__ == "__main__":
    main()
