"""Dump the proposal queue as a readable text table.

Usage: .venv/bin/python src/review_table.py [status]
Writes data/review_table.txt. Default status: pending.
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "proposals.db"
OUT = ROOT / "data" / "review_table.txt"


def fmt_loc(loc):
    return (f"{loc.get('name')} | {loc.get('street_address')}, {loc.get('city')}, "
            f"{loc.get('state')} {loc.get('zip')} | care {loc.get('care_offerings')}")


def fmt_acct(a):
    return (f"{a.get('name')} (#{str(a.get('id', ''))[-6:]}) | "
            f"{a.get('street_address')}, {a.get('city')}, {a.get('state')} {a.get('zip')} | "
            f"parent {str(a.get('parent_id') or 'none')[-6:]} | {a.get('status')} | "
            f"rev {a.get('lifetime_revenue')} ar {a.get('outstanding_ar')} | "
            f"care {a.get('care_offerings')}")


def main():
    status = sys.argv[1] if len(sys.argv) > 1 else "pending"
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM proposals WHERE status=? ORDER BY kind, subject_key",
        (status,)).fetchall()
    lines = [f"# Proposal queue — status={status} — {len(rows)} proposals", ""]
    for r in rows:
        ev = json.loads(r["evidence"])
        ops = json.loads(r["ops"])
        lines.append(f"### [{r['kind']}] {r['subject_key']}  (proposal {r['id'][:8]})")
        if ev.get("location"):
            lines.append(f"  WEBSITE: {fmt_loc(ev['location'])}")
        if ev.get("account"):
            lines.append(f"  CRM: {fmt_acct(ev['account'])}")
        for cand in ev.get("candidates", []) or []:
            lines.append(f"  CANDIDATE: {fmt_acct(cand)} score {cand.get('score')}")
        if ev.get("score") is not None:
            lines.append(f"  SCORE: {ev['score']} {ev.get('breakdown')} tier={ev.get('tier')}")
        if ev.get("reason"):
            lines.append(f"  REASON: {ev['reason']}")
        for note in ev.get("notes", []) or []:
            lines.append(f"  NOTE: {note}")
        if ev.get("diff"):
            lines.append(f"  DIFF: {json.dumps(ev['diff'])}")
        for op in ops:
            if op["type"] == "create":
                f = op["fields"]
                lines.append(f"  OP CREATE: {f.get('name')} | {f.get('street_address')}, "
                             f"{f.get('city')}, {f.get('state')} {f.get('zip')} | "
                             f"parent ...{str(f.get('parent_id'))[-6:]} | care {f.get('care_offerings')}")
            else:
                lines.append(f"  OP {op['type'].upper()} account ...{str(op.get('account_id'))[-6:]}: "
                             f"{json.dumps(op.get('fields', {}))[:220]}")
        lines.append("")
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT} with {len(rows)} proposals (status={status})")


if __name__ == "__main__":
    main()
