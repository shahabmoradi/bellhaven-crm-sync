"""Regression tests for the round-3 / independent-audit fixes:

- a preserved CHOW original is never re-proposed against (the
  post-split re-run trap: Marietta's old account must not come back
  as a "duplicate" of the account the split created)
- Gap A: a cross-operator duplicate loser with revenue > 0 AND
  AR > 0 is CHOW-linked only, never flagged duplicate / inactivated
- confirmed non-match: a rejected ambiguous pair falls through to a
  create on the next run
- Gap B: address diffs compare normalized forms ("St" vs "Street",
  "Northwest" vs "NW", state name vs code, ZIP+4) — cosmetic
  spellings are not fixes; a PO Box vs a real street still is
- executor: create-guard returns the raw record's account_id;
  a direct re-parent is blocked when the CHOW gate has flipped;
  the stale check watches address fields
- final review: a translated op field outside the CRM schema fails
  before any write; plain creates carry a creation note; the store
  retires an older pending proposal that overlaps an account a newer
  run re-proposed (the stale Findlay inactivation shape)

Run: .venv/bin/python tests/test_fixes.py
"""
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from proposals import build_proposals, _field_fixes
import executor
import crm

ROOT = Path(__file__).resolve().parent.parent
locations = json.loads((ROOT / "data" / "locations.json").read_text())
by_name = {l["name"]: l for l in locations}
marietta = by_name["Bellhaven of Marietta"]
dayton = by_name["The Arbors at Bellhaven - Dayton"]

PARENT = {"id": 1, "name": "Bellhaven Senior Living", "parent_id": None,
          "status": "Active", "street_address": "400 Bellhaven Way",
          "city": "Columbus", "state": "OH", "zip": "43085"}
OTHER = {"id": 99, "name": "OtherCare Holdings", "parent_id": None, "status": "Active"}


def ops_on(proposals, account_id):
    return [op for p in proposals for op in p["ops"]
            if op.get("account_id") == account_id]


# --- 1. Post-split re-run: the preserved original is untouchable ----
post_split = [
    PARENT, OTHER,
    # The account the Marietta split CREATED (website values, our parent).
    {"id": 51, "name": "Bellhaven of Marietta", "parent_id": 1, "status": "Active",
     "street_address": marietta["street_address"], "city": marietta["city"],
     "state": marietta["state"], "zip": marietta["zip"],
     "care_offerings": ["Skilled Nursing"],
     "lifetime_revenue": 0, "outstanding_ar": 0},
    # The PRESERVED original: other operator's parent, money history,
    # chow_current_account pointing at the new account.
    {"id": 50, "name": "Bellhaven of Marietta", "parent_id": 99, "status": "Active",
     "street_address": marietta["street_address"], "city": marietta["city"],
     "state": marietta["state"], "zip": marietta["zip"],
     "care_offerings": ["Skilled Nursing"],
     "lifetime_revenue": 51250, "outstanding_ar": 3800,
     "chow_current_account": 51},
]
props, summary = build_proposals(locations, post_split, "2026-10-31")
assert not ops_on(props, 50), ops_on(props, 50)
assert all(p["subject_key"] != marietta["slug"] for p in props), \
    [p for p in props if p["subject_key"] == marietta["slug"]]
assert any(c["account_id"] == 51 for c in summary["confident_no_change"])
print("preserved CHOW original OK: re-run proposes nothing against it")

# --- 2. Gap A: cross-operator loser with money is linked, not killed -
def dayton_cluster(twin_ar):
    return [
        PARENT, OTHER,
        {"id": 60, "name": "The Arbors at Bellhaven - Dayton", "parent_id": 1,
         "status": "Active", "street_address": dayton["street_address"],
         "city": dayton["city"], "state": dayton["state"], "zip": dayton["zip"],
         "care_offerings": dayton["care_offerings"],
         "lifetime_revenue": 0, "outstanding_ar": 0},
        {"id": 61, "name": "Arbors Dayton (OtherCare)", "parent_id": 99,
         "status": "Active", "street_address": dayton["street_address"],
         "city": dayton["city"], "state": dayton["state"], "zip": dayton["zip"],
         "care_offerings": dayton["care_offerings"],
         "lifetime_revenue": 9000, "outstanding_ar": twin_ar},
    ]

props, _ = build_proposals(locations, dayton_cluster(1200), "2026-10-31")
twin_ops = ops_on(props, 61)
assert len(twin_ops) == 1 and twin_ops[0]["fields"] == {"chow_current_account": 60}, twin_ops
print("Gap A OK: money-carrying prior-operator twin gets the CHOW link only")

props, _ = build_proposals(locations, dayton_cluster(0), "2026-10-31")
twin_ops = ops_on(props, 61)
assert len(twin_ops) == 1 and twin_ops[0]["fields"]["duplicate_of_account"] == 60 \
    and twin_ops[0]["fields"]["status"] == "Inactive", twin_ops
print("Gap A control OK: $0-AR twin still takes the duplicate path")

# --- 3. Confirmed non-match falls through to a create ---------------
testville = {"name": "Bellhaven of Testville", "slug": "testville",
             "street_address": "118 Union Square Dr", "city": "New Albany",
             "state": "OH", "zip": "43054",
             "care_offerings": ["Assisted Living"], "phone": "(614) 555-0100"}
not_ours = {"id": 70, "name": "Testville Senior Living", "parent_id": 99,
            "status": "Active", "street_address": "240 Market St",
            "city": "New Albany", "state": "OH", "zip": "43054",
            "care_offerings": ["Assisted Living"], "phone": "(614) 555-0100",
            "lifetime_revenue": 0, "outstanding_ar": 0}
props, _ = build_proposals([testville], [PARENT, OTHER, not_ours], "2026-10-31")
assert [p["kind"] for p in props] == ["ambiguous"], props
props, _ = build_proposals([testville], [PARENT, OTHER, not_ours], "2026-10-31",
                           non_matches={("testville", "70")})
assert [p["kind"] for p in props] == ["create"], props
print("non-match OK: rejected ambiguous pair -> create on the next run")

# --- 4. Gap B: normalized address diffing -----------------------------
loc = {"name": "X", "street_address": "4500 Northwest Blvd", "city": "Toledo",
       "state": "OH", "zip": "43623", "care_offerings": None}
acct = {"name": "X", "street_address": "4500 NW Boulevard", "city": "Toledo",
        "state": "Ohio", "zip": "43623-1234", "status": "Active",
        "care_offerings": []}
fields, diff = _field_fixes(loc, acct)
assert fields == {} and diff == [], (fields, diff)
loc2 = dict(loc, street_address="123 Main Street")
acct2 = dict(acct, street_address="123 Main St", state="OH", zip="43623")
fields, diff = _field_fixes(loc2, acct2)
assert "street_address" not in fields, (fields, diff)
loc3 = dict(loc, street_address="3156 W Prospect Rd")
acct3 = dict(acct, street_address="PO Box 517")
fields, diff = _field_fixes(loc3, acct3)
assert fields.get("street_address") == "3156 W Prospect Rd", (fields, diff)
print("Gap B OK: cosmetic spellings are not fixes; a PO Box still is")

# --- 5. Executor: create-guard reads account_id ----------------------
crm.write_log = lambda entry: None
crm.fetch_all_accounts = lambda: [
    {"account_id": "A1", "name": "Bellhaven of Batavia", "parent_id": "P1",
     "billing_street": "100 Main St", "billing_city": "Batavia",
     "billing_state": "OH", "billing_zip": "45103",
     "care_type": "Assisted Living", "status": "Active"},
]
def no_create(fields):
    raise AssertionError("create_account must not be called on a guard hit")
crm.create_account = no_create
guard_prop = {"id": "t1", "kind": "create", "evidence": {},
              "ops": [{"type": "create",
                       "fields": {"name": "Bellhaven of Batavia",
                                  "street_address": "100 Main Street",
                                  "city": "Batavia", "state": "OH",
                                  "zip": "45103", "parent_id": "P1",
                                  "status": "Active"},
                       "guard": {"house": "100", "street": "main st",
                                 "zip": "45103", "parent_id": "P1"}}]}
out = executor.execute(guard_prop)
assert out["created_id"] == "A1", out
print("executor guard OK: existing account linked, not duplicated")

# --- 6. Executor: direct re-parent blocked when the gate flips -------
record = {"id": 7, "name": "Lima-like", "parent_id": 99, "status": "Active",
          "lifetime_revenue": 47000, "outstanding_ar": 1000}
crm.fetch_account = lambda aid: dict(record)
def no_patch(aid, fields):
    raise AssertionError("patch must not run once the gate has flipped")
crm.patch_account = no_patch
reparent = {"id": "t2", "kind": "fix",
            "evidence": {"before": {"7": {"parent_id": 99}}},
            "ops": [{"type": "patch", "account_id": 7,
                     "fields": {"parent_id": 1}}]}
try:
    executor.execute(reparent)
    raise AssertionError("expected StaleError")
except executor.StaleError:
    pass
record_ok = dict(record, outstanding_ar=0)
crm.fetch_account = lambda aid: dict(record_ok)
patched = []
def ok_patch(aid, fields):
    patched.append((aid, fields))
    record_ok.update(fields)
    return dict(record_ok)
crm.patch_account = ok_patch
out = executor.execute(reparent)
assert patched and out["done_ops"] == 1, out
print("executor gate OK: flipped gate blocks the move; $0 AR still moves")

# --- 7. Executor: stale check watches address fields ------------------
crm.fetch_account = lambda aid: {"id": 8, "name": "Y", "parent_id": 1,
                                 "status": "Active",
                                 "street_address": "9 Changed St"}
street_prop = {"id": "t3", "kind": "fix",
               "evidence": {"before": {"8": {"street_address": "1 Old St"}}},
               "ops": [{"type": "patch", "account_id": 8,
                        "fields": {"street_address": "5 New St"}}]}
try:
    executor.execute(street_prop)
    raise AssertionError("expected StaleError")
except executor.StaleError:
    pass
print("executor stale check OK: address changes are watched")

# --- 8. Independent-build reproductions: full-snapshot stale check ---
# (a) A duplicate LOSER whose money turns positive after the proposal
# was built must NOT be stamped duplicate + Inactive: the snapshot's
# money fields are compared at apply time, the proposal goes stale,
# and a re-run picks the right branch (CHOW link for a prior
# operator's record). Zero writes may happen.
props, _ = build_proposals(locations, dayton_cluster(0), "2026-10-31")
dup_prop = next(p for p in props if p["subject_key"] == dayton["slug"])
twin_now = {"id": 61, "name": "Arbors Dayton (OtherCare)", "parent_id": 99,
            "status": "Active", "street_address": dayton["street_address"],
            "city": dayton["city"], "state": dayton["state"],
            "zip": dayton["zip"], "care_offerings": dayton["care_offerings"],
            "lifetime_revenue": 100, "outstanding_ar": 50}
survivor_now = {"id": 60, "name": "The Arbors at Bellhaven - Dayton",
                "parent_id": 1, "status": "Active",
                "street_address": dayton["street_address"],
                "city": dayton["city"], "state": dayton["state"],
                "zip": dayton["zip"],
                "care_offerings": dayton["care_offerings"],
                "lifetime_revenue": 0, "outstanding_ar": 0}
fresh_map = {61: twin_now, 60: survivor_now}
crm.fetch_account = lambda aid: dict(fresh_map[aid])
writes = []
crm.patch_account = lambda aid, fields: writes.append((aid, fields))
crm.fetch_all_accounts = lambda: []
try:
    executor.execute(dup_prop)
    raise AssertionError("expected StaleError for the money-flipped loser")
except executor.StaleError:
    pass
assert writes == [], writes
print("loser gate OK: money appearing on a loser blocks the duplicate stamp")

# (b) A parent-only proposal never patches the street — but the street
# is its identity evidence. If the account's address changed after
# the proposal was built, approving must stop, not re-parent blind.
lima_like = {"id": 7, "name": "Bellhaven of Marietta", "parent_id": 99,
             "status": "Active",
             "street_address": marietta["street_address"],
             "city": marietta["city"], "state": marietta["state"],
             "zip": marietta["zip"],
             "care_offerings": ["Skilled Nursing"],
             "lifetime_revenue": 47000, "outstanding_ar": 0}
props, _ = build_proposals([marietta], [PARENT, OTHER, lima_like], "2026-10-31")
move_prop = next(p for p in props if p["subject_key"] == marietta["slug"])
assert all("street_address" not in (op.get("fields") or {}) for op in move_prop["ops"])
moved = dict(lima_like, street_address="999 Elsewhere St")
crm.fetch_account = lambda aid: dict(moved)
try:
    executor.execute(move_prop)
    raise AssertionError("expected StaleError for the moved identity anchor")
except executor.StaleError:
    pass
assert writes == [], writes
print("identity anchor OK: an unchanged-field change still blocks approval")

# --- 9. Executor: unknown wire field fails BEFORE any write ---------
# The proposals a reviewer sees are canonical; the executor translates
# them to the CRM's billing_* / care_type schema. A field that does
# not exist in that schema (here a canonical "zip_code" the WRITE_MAP
# does not know) must stop the op, not reach the API.
writes.clear()
crm.fetch_account = lambda aid: {"id": 8, "name": "Y", "parent_id": 1,
                                 "status": "Active"}
bad_prop = {"id": "t9", "kind": "fix", "evidence": {},
            "ops": [{"type": "patch", "account_id": 8,
                     "fields": {"zip_code": "99999"}}]}
try:
    executor.execute(bad_prop)
    raise AssertionError("expected StaleError for unknown field")
except executor.StaleError:
    pass
assert writes == [], writes
bad_create = {"id": "t9b", "kind": "create", "evidence": {},
              "ops": [{"type": "create",
                       "fields": {"name": "X", "bogus_field": 1},
                       "guard": {"house": "1", "street": "main",
                                 "zip": "00000", "parent_id": 1}}]}
try:
    executor.execute(bad_create, dry_run=True)
    raise AssertionError("expected StaleError for unknown create field")
except executor.StaleError:
    pass
print("unknown-field guard OK: bad wire fields never reach the API")

# --- 10. Store: cross-run overlap retires the older pending ---------
import tempfile
import store
conn = store.connect(str(Path(tempfile.mkdtemp()) / "t.db"))
old = {"id": "old1", "kind": "inactivate", "subject_key": "account:X",
       "ops": [{"type": "patch", "account_id": "X", "fields": {"status": "Inactive"}}],
       "evidence": {}}
new = {"id": "new1", "kind": "fix", "subject_key": "some-location",
       "ops": [{"type": "patch", "account_id": "X", "fields": {"parent_id": 1}}],
       "evidence": {}}
other = {"id": "oth1", "kind": "fix", "subject_key": "other-location",
         "ops": [{"type": "patch", "account_id": "Y", "fields": {"name": "Z"}}],
         "evidence": {}}
store.insert_new(conn, [old, other], "run-old")
store.insert_new(conn, [new], "run-new")
assert store.supersede_overlapping_pending(conn, "run-new") == 1
states = {p["id"]: p["status"] for p in store.list_proposals(conn)}
assert states == {"old1": "superseded", "new1": "pending",
                  "oth1": "pending"}, states
print("overlap sweep OK: older overlapping pending retired, rest kept")

# --- 11. Plain creates carry a creation note -------------------------
props, _ = build_proposals([dayton], [PARENT, OTHER], "2026-10-31")
create_prop = next(p for p in props if p["kind"] == "create")
note = create_prop["ops"][0]["fields"]["note"]
assert note.startswith("[2026-10-31] Created from website listing"), note
print("create note OK: new accounts say where they came from")

# --- 12. Post-execution re-run: resolved duplicates stay resolved ---
# After the Kettering cluster was approved and applied, the survivor
# is renamed + parented and both losers are Inactive with
# duplicate_of_account pointing at it. A re-run must propose NOTHING
# for the location — it is a confident no-change match. (The first
# post-execution re-run re-proposed the losers alone, because the
# survivor-side ops had dropped out of the target.)
import normalize as _norm
kettering = by_name["Bellhaven of Kettering"]
_k_care = [_norm.WEB_TO_CRM_CARE.get(o, o) for o in kettering["care_offerings"][:1]]
def _k_acct(id_, name, parent, **kw):
    base = {"id": id_, "name": name, "parent_id": parent, "status": "Active",
            "street_address": kettering["street_address"],
            "city": kettering["city"], "state": kettering["state"],
            "zip": kettering["zip"], "care_offerings": _k_care,
            "lifetime_revenue": 0, "outstanding_ar": 0}
    base.update(kw)
    return base
post_exec = [
    PARENT, OTHER,
    _k_acct(70, "Bellhaven of Kettering", 1),
    _k_acct(71, "Kettering Care Centre", 99, status="Inactive",
            duplicate_of_account=70),
    _k_acct(72, "Kettering Senior Campus", 98, status="Inactive",
            duplicate_of_account=70),
]
props, summary = build_proposals([kettering], post_exec, "2026-10-31")
assert not props, props
assert any(c["account_id"] == 70 for c in summary["confident_no_change"])
print("post-execution re-run OK: resolved duplicate cluster is a no-change match")

print("ALL FIX TESTS PASSED")
