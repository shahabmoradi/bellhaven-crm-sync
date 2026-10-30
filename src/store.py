"""SQLite store for proposals and their review decisions.

A proposal's stable id (see proposals.py) is the primary key. Once a
proposal has been decided (approved / rejected / applied), re-running
the pipeline never resurrects it: insert_new() only adds proposals
whose id is not already in the table.
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "proposals.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id          TEXT PRIMARY KEY,
    run_id      TEXT,
    kind        TEXT,
    subject_key TEXT,
    ops         TEXT,   -- JSON
    evidence    TEXT,   -- JSON
    status      TEXT DEFAULT 'pending',  -- pending|approved|rejected|applied|failed|stale
    created_at  TEXT,
    decided_at  TEXT,
    reason      TEXT,   -- rejection reason (required on reject)
    result      TEXT    -- JSON: executor outcome, created ids, errors
);
"""


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def insert_new(conn, proposals, run_id):
    """Insert proposals whose stable id is not already known.
    Pending proposals for the same subject whose target has changed
    (different stable id) are marked superseded, so the queue never
    offers an outdated change. Returns (inserted, skipped, superseded)."""
    inserted = skipped = superseded = 0
    for p in proposals:
        cur = conn.execute(
            "UPDATE proposals SET status = 'superseded' "
            "WHERE subject_key = ? AND id != ? AND status = 'pending'",
            (p["subject_key"], p["id"]),
        )
        superseded += cur.rowcount
        cur = conn.execute(
            "INSERT OR IGNORE INTO proposals (id, run_id, kind, subject_key, ops, evidence, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (p["id"], run_id, p["kind"], p["subject_key"],
             json.dumps(p["ops"]), json.dumps(p["evidence"]), _now()),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return inserted, skipped, superseded


def list_proposals(conn, status=None):
    if status:
        rows = conn.execute("SELECT * FROM proposals WHERE status = ? ORDER BY kind, subject_key",
                            (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM proposals ORDER BY kind, subject_key").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ops"] = json.loads(d["ops"])
        d["evidence"] = json.loads(d["evidence"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        out.append(d)
    return out


def get(conn, proposal_id):
    rows = list_proposals(conn)
    for p in rows:
        if p["id"] == proposal_id:
            return p
    return None


def counts(conn):
    rows = conn.execute("SELECT status, COUNT(*) c FROM proposals GROUP BY status").fetchall()
    by_status = {r["status"]: r["c"] for r in rows}
    rows = conn.execute("SELECT kind, COUNT(*) c FROM proposals GROUP BY kind").fetchall()
    by_kind = {r["kind"]: r["c"] for r in rows}
    return {"by_status": by_status, "by_kind": by_kind}


def save_result(conn, proposal_id, result):
    """Persist executor progress without changing the status
    (used by the executor's per-op save_progress callback)."""
    conn.execute("UPDATE proposals SET result = ? WHERE id = ?",
                 (json.dumps(result), proposal_id))
    conn.commit()


def set_status(conn, proposal_id, status, reason=None, result=None):
    if result is None:
        conn.execute(
            "UPDATE proposals SET status = ?, decided_at = ?, reason = ? WHERE id = ?",
            (status, _now(), reason, proposal_id),
        )
    else:
        conn.execute(
            "UPDATE proposals SET status = ?, decided_at = ?, reason = ?, result = ? WHERE id = ?",
            (status, _now(), reason, json.dumps(result), proposal_id),
        )
    conn.commit()
