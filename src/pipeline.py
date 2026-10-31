"""Pipeline entry point: scrape -> snapshot -> match -> propose.

    python3 src/pipeline.py run [--fixture ACCOUNTS_JSON]

Normal mode reads the live CRM (token required) and snapshots every
account to data/snapshot_before.json before anything else happens —
there is no delete in this API, so the snapshot is the audit trail.
Fixture mode runs the same matching/proposal code against a local
accounts file, for offline testing.

This module is READ-ONLY against the CRM. Writes happen only in
executor.py, only from the review app's approve route.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crm
import store
from proposals import build_proposals

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_locations():
    path = DATA / "locations.json"
    if not path.exists():
        raise SystemExit("data/locations.json missing — run src/scrape.py first")
    return json.loads(path.read_text())


def run(fixture=None):
    locations = load_locations()
    if fixture:
        accounts = json.loads(Path(fixture).read_text())
        print(f"fixture mode: {len(accounts)} accounts from {fixture}")
    else:
        accounts = crm.fetch_all_accounts()
        snap = DATA / "snapshot_before.json"
        if not snap.exists():
            snap.write_text(json.dumps(accounts, indent=2))
            print(f"snapshot saved: {snap} ({len(accounts)} accounts)")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (DATA / f"snapshot_{stamp}.json").write_text(json.dumps(accounts, indent=2))
        print(f"fetched {len(accounts)} accounts from the CRM")

    today = time.strftime("%Y-%m-%d")
    conn = store.connect()
    non_matches = store.confirmed_non_matches(conn)
    if non_matches:
        print(f"confirmed non-matches in force: {sorted(non_matches)}")
    proposals, summary = build_proposals(locations, accounts, today,
                                         non_matches=non_matches)
    run_id = time.strftime("%Y-%m-%dT%H:%M:%S")
    inserted, skipped, superseded = store.insert_new(conn, proposals, run_id)
    swept = store.supersede_pending_subjects(conn, summary["confident_subjects"])
    if swept:
        print(f"swept {swept} stale pending proposals for locations now "
              f"classified confident (no change needed)")
    overlap = store.supersede_overlapping_pending(conn, run_id)
    if overlap:
        print(f"retired {overlap} stale pending proposal(s) that overlapped "
              f"an account re-proposed by this run")
    print(f"parent: {summary['parent_id']} ({summary['parent_note']})")
    print(f"locations: {summary['locations']}  proposals: {len(proposals)} "
          f"by kind: {summary['proposal_counts']}")
    print(f"confident, no change needed: {len(summary['confident_no_change'])}")
    print(f"inserted {inserted} new proposals; {skipped} already known "
          f"(decided or pending); {superseded} pending superseded by changed targets")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["run"])
    ap.add_argument("--fixture", help="local accounts JSON for offline testing")
    args = ap.parse_args()
    if args.command == "run":
        run(fixture=args.fixture)
