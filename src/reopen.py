"""Reopen a decided proposal, with an audit trail.

    python3 src/reopen.py <proposal_id> --reason "why"

Use case: a proposal was rejected during the demo recording but is
actually correct. Reopening returns it to pending so it can be
approved afterwards; the reopen (with its reason) is appended to the
proposal's audit history, never hidden.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store

REOPENABLE = ("rejected", "failed", "stale", "superseded", "applied")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("proposal_id")
    ap.add_argument("--reason", required=True)
    args = ap.parse_args()

    conn = store.connect()
    proposal = store.get(conn, args.proposal_id)
    if not proposal:
        raise SystemExit(f"no proposal with id {args.proposal_id}")
    if proposal["status"] not in REOPENABLE:
        raise SystemExit(f"proposal is {proposal['status']}; nothing to reopen")

    result = proposal.get("result") or {}
    audit = result.get("audit", [])
    audit.append({
        "action": "reopened",
        "from_status": proposal["status"],
        "reason": args.reason,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    result["audit"] = audit
    # A reopened proposal starts its ops fresh: clear execution progress
    # (the create-guard and stale checks protect against double effects).
    result.pop("done_ops", None)
    result.pop("created_id", None)
    result.pop("log", None)
    conn.execute(
        "UPDATE proposals SET status = 'pending', reason = NULL, result = ? WHERE id = ?",
        (json.dumps(result), args.proposal_id),
    )
    conn.commit()
    print(f"reopened {args.proposal_id} (was {proposal['status']}): {args.reason}")


if __name__ == "__main__":
    main()
