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

print("ALL FIX TESTS PASSED")
