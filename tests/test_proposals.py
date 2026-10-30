"""End-to-end offline test: fixture CRM accounts + the real scraped
locations -> proposals must cover every case type; the store must be
idempotent; the executor must substitute $created_id on a CHOW split.

Run: .venv/bin/python tests/test_proposals.py
"""
import json
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from proposals import build_proposals
import store
import executor
import crm

ROOT = Path(__file__).resolve().parent.parent
locations = json.loads((ROOT / "data" / "locations.json").read_text())
by_name = {l["name"]: l for l in locations}
amberly = by_name["Amberly Manor"]            # Hudson OH, no Bellhaven in the name
dayton = by_name["The Arbors at Bellhaven - Dayton"]

PARENT = {"id": 1, "name": "Bellhaven Senior Living", "parent_id": None,
          "status": "Active", "street_address": "400 Bellhaven Way",
          "city": "Columbus", "state": "OH", "zip": "43085"}

accounts = [
    PARENT,
    # Amberly: exact match, correct parent -> no proposal expected for it.
    {"id": 10, "name": "Amberly Manor", "parent_id": 1, "status": "Active",
     "street_address": amberly["street_address"], "city": amberly["city"],
     "state": amberly["state"], "zip": amberly["zip"],
     "care_offerings": amberly["care_offerings"],
     "lifetime_revenue": 5000, "outstanding_ar": 0},
    # Dayton: TWO accounts at the same address (duplicate cluster);
    # the survivor sits under the WRONG parent with revenue + AR,
    # so after dedup it must take the CHOW path.
    {"id": 20, "name": "The Arbors at Bellhaven - Dayton", "parent_id": 99, "status": "Active",
     "street_address": dayton["street_address"], "city": dayton["city"],
     "state": dayton["state"], "zip": dayton["zip"],
     "care_offerings": dayton["care_offerings"],
     "lifetime_revenue": 12400, "outstanding_ar": 3100, "updated_at": "2026-10-01"},
    {"id": 21, "name": "Arbors Dayton", "parent_id": None, "status": "Inactive",
     "street_address": dayton["street_address"], "city": dayton["city"],
     "state": dayton["state"], "zip": dayton["zip"],
     "lifetime_revenue": 0, "outstanding_ar": 0, "updated_at": "2025-01-01"},
    # A different operator's parent + one of its facilities (not ours).
    {"id": 99, "name": "OtherCare Holdings", "parent_id": None, "status": "Active"},
    # Bellhaven child that is not on the website anymore -> inactivate.
    {"id": 30, "name": "Bellhaven of Nowhere", "parent_id": 1, "status": "Active",
     "street_address": "1 Ghost Road", "city": "Nowhere", "state": "OH", "zip": "44000",
     "lifetime_revenue": 0, "outstanding_ar": 250},
]

proposals, summary = build_proposals(locations, accounts, "2026-10-30")
kinds = {}
for p in proposals:
    kinds.setdefault(p["kind"], []).append(p)

assert summary["parent_id"] == 1, summary
assert "chow_split" in kinds, kinds.keys()
chow = kinds["chow_split"][0]
op_types = [op["type"] for op in chow["ops"]]
assert op_types[0] == "patch" and chow["ops"][0]["fields"]["duplicate_of_account"] == 20, chow["ops"]
assert "create" in op_types and chow["ops"][-1]["fields"]["chow_current_account"] == "$created_id", chow["ops"]
assert "inactivate" in kinds and kinds["inactivate"][0]["subject_key"] == "account:30"
creates = [p for p in proposals if p["kind"] == "create"]
assert len(creates) == 32, f"expected 32 creates (34 locations - amberly - dayton), got {len(creates)}"
assert all(p["subject_key"] != (amberly.get('slug') or '') for p in proposals if p["kind"] == "create")
assert len({p["id"] for p in proposals}) == len(proposals), "proposal ids must be unique"
print("proposal kinds:", {k: len(v) for k, v in kinds.items()})

# Store idempotency: second insert adds nothing.
with tempfile.TemporaryDirectory() as td:
    conn = store.connect(Path(td) / "t.db")
    a, _, _ = store.insert_new(conn, proposals, "run1")
    b, skipped, _ = store.insert_new(conn, proposals, "run2")
    assert a == len(proposals) and b == 0 and skipped == len(proposals)
    # Supersede: a changed target for the same subject retires the pending one.
    changed = [dict(proposals[0], id="f" * 24,
                    ops=[{"type": "patch", "account_id": 424242, "fields": {"status": "Inactive"}}])]
    c, _, sup = store.insert_new(conn, changed, "run3")
    assert c == 1 and sup == 1, (c, sup)
    assert store.get(conn, proposals[0]["id"])["status"] == "superseded"
print("store idempotency + supersede OK")

# Executor dry-run with stubbed reads: CHOW create id flows into the patch.
crm.fetch_account = lambda aid: next(a for a in accounts if a["id"] == aid)
result = executor.execute(chow, dry_run=True)
patch_ops = [l for l in result["log"] if l["op"] == "patch"]
chow_patch = [l for l in patch_ops if l["account_id"] == 20][0]
assert chow_patch["fields"]["chow_current_account"] == -1, chow_patch  # dry-run placeholder id
print("executor dry-run OK:", [l["op"] for l in result["log"]])

# Resume after a mid-list failure: op 2 (the create) succeeds, op 3
# (the CHOW patch) fails once. The retry must NOT create a second
# account — it resumes with the stored created id.
creates_made = []
state = {"fail_patch": True}
crm.write_log = lambda entry: None
crm.fetch_all_accounts = lambda: []
def fake_create(fields):
    creates_made.append(fields)
    return {"id": 777, **fields}
def fake_patch(aid, fields):
    if state["fail_patch"] and fields.get("chow_current_account") == 777:
        state["fail_patch"] = False
        raise RuntimeError("simulated network failure")
    record = next(a for a in accounts if a["id"] == aid)
    record.update(fields)  # stateful, so the executor's verify step passes
    return {"id": aid, **fields}
crm.create_account = fake_create
crm.patch_account = fake_patch
progress_box = {}
def save_progress(progress):
    progress_box.update(progress)
try:
    executor.execute(chow, save_progress=save_progress)
    raise AssertionError("first attempt should have failed")
except RuntimeError:
    pass
assert progress_box.get("done_ops") == 2 and progress_box.get("created_id") == 777, progress_box
retry = dict(chow, result={"done_ops": progress_box["done_ops"],
                           "created_id": progress_box["created_id"],
                           "log": progress_box["log"]})
out = executor.execute(retry, save_progress=save_progress)
assert len(creates_made) == 1, creates_made
assert out["done_ops"] == len(chow["ops"])
print("executor resume-after-failure OK: exactly one create across retry")

# Amberly Manor: the no-brand-name location under a former operator's
# parent with revenue + AR must come out as a CHOW split (address
# carries the match; brand stripping handles the name).
amberly_accts = [
    PARENT,
    {"id": 99, "name": "OtherCare Holdings", "parent_id": None, "status": "Active"},
    {"id": 40, "name": "Amberly Manor", "parent_id": 99, "status": "Active",
     "street_address": amberly["street_address"], "city": amberly["city"],
     "state": amberly["state"], "zip": amberly["zip"],
     "care_offerings": amberly["care_offerings"],
     "lifetime_revenue": 8000, "outstanding_ar": 900},
]
a_props, _ = build_proposals(locations, amberly_accts, "2026-10-30")
amberly_prop = next(p for p in a_props if p["subject_key"] == amberly["slug"])
assert amberly_prop["kind"] == "chow_split", amberly_prop["kind"]
print("Amberly Manor named test OK: CHOW split under the former operator")

# Care subset rule: the CRM care field holds ONE label. If the label
# it holds is among the website's offerings, there is no care diff
# (the field cannot represent the rest) — even for a dual-offering
# facility. A disjoint label WOULD be a diff.
from proposals import _field_fixes
erie_loc = next(l for l in locations if l["name"] == "Bellhaven Shores of Erie")
assert set(erie_loc["care_offerings"]) == {"Assisted Living", "Memory Support"}
_, diff_ok = _field_fixes(erie_loc, {"name": "Bellhaven Shores of Erie",
                                     "street_address": erie_loc["street_address"],
                                     "city": erie_loc["city"], "state": erie_loc["state"],
                                     "zip": erie_loc["zip"], "status": "Active",
                                     "care_offerings": ["Assisted Living"]})
assert not any(d[0] == "care_offerings" for d in diff_ok), diff_ok
_, diff_bad = _field_fixes(erie_loc, {"name": "Bellhaven Shores of Erie",
                                      "street_address": erie_loc["street_address"],
                                      "city": erie_loc["city"], "state": erie_loc["state"],
                                      "zip": erie_loc["zip"], "status": "Active",
                                      "care_offerings": ["Skilled Nursing"]})
assert any(d[0] == "care_offerings" for d in diff_bad), diff_bad
print("care subset rule OK: held label among website offerings = no diff")
print("ALL PROPOSAL TESTS PASSED")
