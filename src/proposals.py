"""Proposal builder: turn match results into ordered, reviewable changes.

One proposal per website location, holding an ordered list of operations
that a reviewer approves or rejects as a unit. Plus one proposal per
Bellhaven CRM account that no longer appears on the website.

Decision order (this order matters and is deliberate):
  0. Resolve P, the canonical Bellhaven parent account.
  1. Duplicates first: if several accounts anchor to one location, pick
     the survivor (under P > Active > has financial history > most
     recently updated > lowest id). Losers get duplicate_of_account =
     survivor and status Inactive — with ONE exception: a loser under
     ANOTHER operator (non-null, non-Bellhaven parent) whose revenue
     AND outstanding AR are both > 0 is that operator's billing record
     for the same facility, not a duplicate. The CHOW gate applies to
     it exactly as it would to a survivor: its only change is
     chow_current_account = survivor; no duplicate flag, no status
     change. (Verified on this dataset: every cross-operator loser is
     $0/$0, so the exception changes no outcome here — the gate exists
     so the code is right by construction.)
     An account that already carries chow_current_account is a
     PRESERVED original from an earlier split: it is historical and
     untouchable. It is excluded from candidacy (its current account
     is the match), never a duplicate loser, never inactivated.
  2. Parent check on the survivor: wrong parent + revenue > 0 AND
     outstanding AR > 0  ->  CHOW split (create a fresh account under P
     and point the old account's chow_current_account at it; the old
     account is otherwise untouched). Otherwise re-parent directly.
     An orphan (parent_id null) is ASSIGNED a parent directly: giving
     an account a parent is not "moving" it, so the CHOW gate does
     not apply. This interpretation is stated in the writeup.
  3. Field fixes (name, address, care offerings, reactivate) apply to
     the current account: the survivor, or the new CHOW account (which
     is created with the website values, so usually nothing to fix).
  4. Locations with no account -> create under P.
  5. Bellhaven accounts matched to nothing -> inactivate proposal.

Every proposal carries a stable idempotency key:
  sha256(kind | subject_key | canonical_json(target ops))
Only the desired end state is hashed — never scores, timestamps, or
note text — so a re-run produces the same key for the same change and
decided items are never re-proposed.
"""

import hashlib
import json

from normalize import (normalize_location, account_view, canonical_account,
                       split_street, city_key, state_code, zip5, WEB_TO_CRM_CARE)
from match import match_location, DUP_CLUSTER_MIN, TIER_FLOOR

TODAY = None  # set by the pipeline (kept injectable for tests)

# Fields snapshotted into every proposal's evidence ("before") so the
# executor's stale check can cover EVERY account a proposal touches —
# survivor, duplicate losers, CHOW source — and every field class,
# address and care included, not just name/parent/status.
SNAP_FIELDS = ("name", "parent_id", "status", "duplicate_of_account",
               "chow_current_account", "street_address", "city", "state",
               "zip", "care_offerings", "lifetime_revenue", "outstanding_ar")


def _snapshot(acct):
    return {f: acct.get(f) for f in SNAP_FIELDS}


def _money(record, key):
    """Parse a money field: numbers pass through; strings like
    "$1,200" are cleaned; null/garbage counts as 0 (flagged at recon)."""
    value = record.get(key)
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _care_set(value):
    """Care offerings as a comparable set, whether the source stores
    a list or a comma/semicolon string, in any case or order."""
    if value is None:
        return set()
    if isinstance(value, str):
        parts = value.replace(";", ",").split(",")
    else:
        parts = list(value)
    return {str(p).strip().lower() for p in parts if str(p).strip()}


def _stable_key(kind, subject_key, ops):
    target = [
        {k: v for k, v in op.items() if k in ("type", "account_id", "fields", "link_field")}
        for op in ops
    ]
    # Note text is cosmetic and date-stamped; exclude it from the target.
    for op in target:
        if isinstance(op.get("fields"), dict):
            op["fields"] = {k: v for k, v in op["fields"].items() if k != "note_append"}
    raw = f"{kind}|{subject_key}|{json.dumps(target, sort_keys=True, default=str)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def resolve_parent(accounts):
    """Find the canonical Bellhaven parent account id.

    The parent is the account literally named as the Bellhaven parent
    account ('Bellhaven Senior Living (Parent Account)'). Falls back
    to the Bellhaven-named account with the most children.
    Returns (parent_id, note) — the note is surfaced for the reviewer.
    """
    for a in accounts:
        name = (a.get("name") or "").strip().lower()
        if name == "bellhaven senior living (parent account)":
            return a.get("id"), "parent chosen: the account named 'Bellhaven Senior Living (Parent Account)'"
    child_count = {}
    for a in accounts:
        pid = a.get("parent_id")
        if pid is not None:
            child_count[pid] = child_count.get(pid, 0) + 1
    by_id = {a.get("id"): a for a in accounts}
    candidates = []
    for pid, count in child_count.items():
        acct = by_id.get(pid)
        if acct and "bellhaven" in (acct.get("name") or "").lower():
            candidates.append((count, pid))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1], f"parent chosen: most Bellhaven-named children ({candidates[0][0]})"
    for a in accounts:
        if a.get("parent_id") is None and "bellhaven" in (a.get("name") or "").lower():
            return a.get("id"), "parent chosen: parentless Bellhaven-named account (no children found)"
    return None, "WARNING: no Bellhaven parent could be resolved"


def _pick_survivor(cluster_raw, parent_id):
    """Choose the surviving account of a duplicate cluster by
    successive filtering, in this order: already under the Bellhaven
    parent; Active; carries financial history; most recently updated;
    lowest id. Readable on purpose — this is explained in the
    walkthrough."""
    pool = list(cluster_raw)
    def keep_best(pred):
        nonlocal pool
        best = [a for a in pool if pred(a)]
        if best:
            pool = best
    keep_best(lambda a: str(a.get("parent_id")) == str(parent_id))
    keep_best(lambda a: (a.get("status") or "").lower() == "active")
    keep_best(lambda a: _money(a, "lifetime_revenue") > 0 or _money(a, "outstanding_ar") > 0)
    if len(pool) > 1:
        latest = max(str(a.get("updated_at") or "") for a in pool)
        pool = [a for a in pool if str(a.get("updated_at") or "") == latest]
    if len(pool) > 1:
        lowest = min(str(a.get("id")) for a in pool)
        pool = [a for a in pool if str(a.get("id")) == lowest]
    return pool[0]


def _field_fixes(loc_raw, acct):
    """Diff website values against a canonical CRM record; return
    patch fields (canonical names; the executor translates them to
    API field names at write time) plus a human-readable diff.

    Address fields are compared on their NORMALIZED form (USPS
    suffixes and directionals, zip5, state code): "3156 W Prospect
    Road" vs "3156 W Prospect Rd" is the same address, not a fix.
    Only substantive differences produce a patch — a different house
    number or street, a PO Box where the website has a real street,
    a wrong zip, a missing value."""
    fields, diff = {}, []
    if (acct.get("name") or "").strip().lower() != (loc_raw.get("name") or "").strip().lower():
        fields["name"] = loc_raw["name"]
        diff.append(("name", acct.get("name"), loc_raw["name"]))

    if loc_raw.get("street_address") is not None:
        want_addr = split_street(loc_raw["street_address"])
        have_addr = split_street(acct.get("street_address") or "")
        if want_addr != have_addr:
            fields["street_address"] = loc_raw["street_address"]
            diff.append(("street_address", acct.get("street_address"),
                         loc_raw["street_address"]))
    if loc_raw.get("city") is not None:
        if city_key(acct.get("city")) != city_key(loc_raw["city"]):
            fields["city"] = loc_raw["city"]
            diff.append(("city", acct.get("city"), loc_raw["city"]))
    if loc_raw.get("state") is not None:
        if state_code(acct.get("state")) != state_code(loc_raw["state"]):
            fields["state"] = loc_raw["state"]
            diff.append(("state", acct.get("state"), loc_raw["state"]))
    if loc_raw.get("zip") is not None:
        if zip5(acct.get("zip")) != zip5(loc_raw["zip"]):
            fields["zip"] = loc_raw["zip"]
            diff.append(("zip", acct.get("zip"), loc_raw["zip"]))

    # Care: the website and CRM use different vocabularies, so the
    # website offerings are mapped to CRM labels before comparing.
    # The CRM field holds ONE label: if the value it already holds is
    # among the website's offerings, there is nothing to fix (the
    # field cannot represent the rest) — a diff is proposed only when
    # the held label is not one of the website's offerings at all.
    if loc_raw.get("care_offerings") is not None:
        want_crm = {WEB_TO_CRM_CARE.get(o, o).lower() for o in loc_raw["care_offerings"]}
        have_crm = _care_set(acct.get("care_offerings"))
        if have_crm and not (have_crm & want_crm):
            fields["care_offerings"] = loc_raw["care_offerings"]
            diff.append(("care_offerings", acct.get("care_offerings"), loc_raw["care_offerings"]))

    if (acct.get("status") or "").lower() == "inactive":
        fields["status"] = "Active"
        diff.append(("status", acct.get("status"), "Active"))
    return fields, diff


def _merge_patches(ops):
    """Merge multiple patch ops on the same account within one
    proposal into a single op (keeping the first op's position).
    A survivor can legitimately need a re-parent AND field fixes;
    the reviewer should see one patch per account. note_append
    texts are joined. (The CHOW patch on an old account is always
    the only patch on that account, so ordering vs the create op
    is unaffected.)"""
    merged, position = [], {}
    for op in ops:
        if op["type"] != "patch":
            merged.append(op)
            continue
        key = str(op["account_id"])
        if key not in position:
            position[key] = len(merged)
            merged.append({"type": "patch", "account_id": op["account_id"],
                           "fields": dict(op.get("fields") or {})})
            continue
        target = merged[position[key]]["fields"]
        for field, value in (op.get("fields") or {}).items():
            if field == "note_append" and "note_append" in target:
                target[field] = target[field] + " " + value
            else:
                target[field] = value
    return merged


def _website_fields(loc_raw, parent_id):
    return {
        "name": loc_raw["name"],
        "street_address": loc_raw.get("street_address"),
        "city": loc_raw.get("city"),
        "state": loc_raw.get("state"),
        "zip": loc_raw.get("zip"),
        "care_offerings": loc_raw.get("care_offerings"),
        "parent_id": parent_id,
        "status": "Active",
    }


def build_proposals(locations, accounts, today, non_matches=None):
    """locations: raw website dicts. accounts: raw API records (they
    are translated to canonical shape here) or canonical dicts.
    non_matches: optional set of (subject_key, account_id) pairs a
    reviewer has CONFIRMED are not the same facility (drawn from
    rejected ambiguous proposals). A confirmed non-match is excluded
    from candidacy, so the location falls through to a create.
    Returns (proposals, summary)."""
    global TODAY
    TODAY = today
    accounts = [canonical_account(a) for a in accounts]
    parent_id, parent_note = resolve_parent(accounts)
    views = [account_view(a) for a in accounts]
    proposals = []
    summary = {"parent_id": parent_id, "parent_note": parent_note,
               "confident_no_change": [], "confident_subjects": [],
               "locations": len(locations)}

    # Preserved originals: an account whose chow_current_account points
    # at another account that still exists is the historical half of a
    # completed CHOW split. It must never be proposed against again —
    # not as a duplicate, not as a fix target, not as an inactivation.
    # Its current account is the facility's live record.
    account_ids = {str(a.get("id")) for a in accounts}
    preserved_ids = {
        str(a.get("id")) for a in accounts
        if a.get("chow_current_account")
        and str(a.get("chow_current_account")) in account_ids
    }
    if preserved_ids:
        summary["preserved_chow_originals"] = sorted(preserved_ids)
    non_matches = non_matches or set()

    # Match every location against ALL accounts (not just Bellhaven
    # children) so wrong-parent and orphan accounts are findable.
    results = []
    for loc_raw in locations:
        loc = normalize_location(loc_raw)
        results.append((loc_raw, loc, match_location(loc, views)))
    # Claim accounts greedily, best scores first, so two locations
    # never take the same account.
    results.sort(key=lambda r: -(r[2]["best"]["score"] if r[2]["best"] else 0))
    claimed = set()

    for loc_raw, loc, result in results:
        slug = loc_raw.get("slug") or loc["name_core"]
        cands = [c for c in result["candidates"] if str(c["account"]["id"]) not in claimed]
        # Preserved CHOW originals are not candidates; their current
        # account (the split's create) is the match for the location.
        cands = [c for c in cands if str(c["account"]["id"]) not in preserved_ids]
        # Confirmed non-matches (reviewer-rejected ambiguous pairs)
        # are not candidates either.
        if non_matches:
            cands = [c for c in cands
                     if (slug, str(c["account"]["id"])) not in non_matches]
        if cands and cands[0]["score"] < TIER_FLOOR:
            # Exclusions removed every plausible candidate; only weak
            # leftovers remain, which is a no-account location.
            cands = []
        if not cands:
            tier = "no_account"
            cluster, best = [], None
        else:
            best = cands[0]
            cluster = [c for c in cands if c["anchored"] and c["score"] >= DUP_CLUSTER_MIN]
            tier = result["tier"]
            if tier == "duplicate_cluster" and len(cluster) < 2:
                tier = "needs_fix" if best["anchored"] else "ambiguous"
            if tier == "ambiguous" and best["anchored"]:
                # Reachable after exclusions: the rival that made this
                # ambiguous is gone, and the best candidate is
                # address-anchored — a fix, not a coin flip.
                tier = "needs_fix"

        guard = {"house": loc["house"], "street": loc["street"],
                 "zip": loc["zip"], "parent_id": parent_id}
        if tier == "no_account" or best is None:
            fields = _website_fields(loc_raw, parent_id)
            ops = [{"type": "create", "fields": fields, "guard": guard}]
            proposals.append({
                "kind": "create", "subject_key": slug, "ops": ops,
                "evidence": {"location": loc_raw, "reason": "No CRM account matched this website location."},
            })
            continue

        if tier == "ambiguous":
            # Approving an ambiguous proposal never picks a winner: it
            # flags the leading candidate Needs Review and names both
            # candidates in the note for a human to resolve.
            top = cands[0]
            for c in cands[:2]:
                claimed.add(str(c["account"]["id"]))
            names = " and ".join(
                f"#{c['account']['id']} (score {c['score']})" for c in cands[:2])
            ops = [{"type": "patch", "account_id": top["account"]["id"],
                    "fields": {"status": "Needs Review",
                               "note_append": f"[{today}] Ambiguous match for website location "
                                              f"'{loc_raw['name']}': candidates {names}. Needs human review."}}]
            proposals.append({
                "kind": "ambiguous", "subject_key": slug, "ops": ops,
                "evidence": {
                    "location": loc_raw,
                    "reason": "No address anchor and no clear winner; reviewer must decide.",
                    "before": {str(top["account"]["id"]): _snapshot(top["account"]["raw"])},
                    "candidates": [
                        {"account_id": c["account"]["id"], "score": c["score"],
                         "breakdown": c["breakdown"], "account": c["account"]["raw"]}
                        for c in cands[:2]
                    ],
                },
            })
            continue

        # A real match. Cluster accounts (dedup) or just the best one.
        cluster_views = cluster if len(cluster) >= 2 else [best]
        cluster_raw = [c["account"]["raw"] for c in cluster_views]
        for c in cluster_views:
            claimed.add(str(c["account"]["id"]))
        survivor = _pick_survivor(cluster_raw, parent_id)
        ops, notes = [], []
        kind = "fix"

        # Step 1: duplicates.
        for loser in cluster_raw:
            if loser.get("id") == survivor.get("id"):
                continue
            if loser.get("chow_current_account"):
                # Defensive: preserved originals are filtered out of
                # candidacy above, so this should be unreachable — but
                # a preserved account is never marked a duplicate.
                notes.append(f"'{loser['name']}' is a preserved CHOW original "
                             f"(chow_current_account set); left untouched.")
                continue
            loser_parent = loser.get("parent_id")
            cross_operator = (loser_parent is not None
                              and str(loser_parent) != str(parent_id))
            l_rev = _money(loser, "lifetime_revenue")
            l_ar = _money(loser, "outstanding_ar")
            if cross_operator and l_rev > 0 and l_ar > 0:
                # Prior-operator twin: the other operator's billing
                # record for this same facility. The CHOW gate applies
                # to it exactly as to a survivor — link it to the
                # current account and change nothing else.
                ops.append({
                    "type": "patch", "account_id": loser["id"],
                    "fields": {"chow_current_account": survivor["id"]},
                })
                notes.append(
                    f"Prior-operator twin: '{loser['name']}' sits under another "
                    f"operator with revenue ${l_rev:,.0f} AND AR ${l_ar:,.0f} > 0, "
                    f"so the CHOW rule governs it — linked via chow_current_account "
                    f"to the survivor only; no duplicate flag, no status change.")
                kind = "duplicate"
                continue
            ops.append({
                "type": "patch", "account_id": loser["id"],
                "fields": {"duplicate_of_account": survivor["id"], "status": "Inactive",
                           "note_append": f"[{today}] Duplicate of account {survivor['id']} "
                                            f"(same facility as website location '{loc_raw['name']}'). Marked Inactive."},
            })
            kind = "duplicate"

        # Step 2: parent / CHOW on the survivor.
        surv_parent = survivor.get("parent_id")
        rev, ar = _money(survivor, "lifetime_revenue"), _money(survivor, "outstanding_ar")
        chow = False
        if survivor.get("chow_current_account"):
            # Defensive (candidacy filtering makes this unreachable in
            # practice): this account is itself a preserved original.
            notes.append("Survivor already carries chow_current_account; "
                         "it is a preserved original — no parent action.")
        elif str(surv_parent) != str(parent_id):
            if surv_parent is None:
                ops.append({"type": "patch", "account_id": survivor["id"],
                            "fields": {"parent_id": parent_id,
                                       "note_append": f"[{today}] Assigned Bellhaven parent (account was an orphan; assignment, not a move)."}})
                notes.append("Orphan account assigned the Bellhaven parent directly.")
            elif rev > 0 and ar > 0:
                chow = True
                kind = "chow_split"
                new_fields = _website_fields(loc_raw, parent_id)
                new_fields["note"] = (f"[{today}] Created by CHOW split from account {survivor['id']} "
                                      f"(ownership change; old account preserved for billing).")
                ops.append({"type": "create", "fields": new_fields, "link_from": survivor["id"],
                            "guard": guard})
                ops.append({"type": "patch", "account_id": survivor["id"],
                            "fields": {"chow_current_account": "$created_id"}})
                notes.append(f"CHOW split: revenue ${rev:,.0f} > 0 AND AR ${ar:,.0f} > 0, so the old "
                             "account is preserved untouched except chow_current_account.")
            else:
                ops.append({"type": "patch", "account_id": survivor["id"],
                            "fields": {"parent_id": parent_id,
                                       "note_append": f"[{today}] Re-parented to Bellhaven "
                                                        f"(revenue ${rev:,.0f}, AR ${ar:,.0f}; CHOW gate not met)."}})
                notes.append(f"Direct re-parent: CHOW gate not met (revenue ${rev:,.0f}, AR ${ar:,.0f}).")

        # Step 3: field fixes on the current account (never the old CHOW account).
        diff = []
        if not chow:
            fields, diff = _field_fixes(loc_raw, survivor)
            if fields:
                fields = dict(fields)
                fields.setdefault("note_append",
                                  f"[{today}] Synced to website listing for '{loc_raw['name']}'.")
                ops.append({"type": "patch", "account_id": survivor["id"], "fields": fields})

        if not ops:
            summary["confident_no_change"].append(
                {"location": loc_raw["name"], "account_id": survivor["id"], "score": best["score"]})
            summary["confident_subjects"].append(slug)
            continue

        proposals.append({
            "kind": kind, "subject_key": slug, "ops": _merge_patches(ops),
            "evidence": {
                "location": loc_raw,
                "account": survivor,
                "before": {str(a["id"]): _snapshot(a) for a in cluster_raw},
                "score": best["score"], "breakdown": best["breakdown"],
                "tier": tier, "notes": notes,
                "diff": diff,
                "financials": {"lifetime_revenue": rev, "outstanding_ar": ar},
                "cluster_account_ids": [a["id"] for a in cluster_raw],
            },
        })

    # Step 5: Bellhaven accounts that matched nothing -> inactivate.
    # Scope: children of the Bellhaven parent, plus Bellhaven-NAMED
    # orphan accounts (no parent at all — a phantom facility that is
    # not on the website either; stated as a judgment call in the
    # writeup). Every account already spoken for (matched, duplicate
    # loser, CHOW source, ambiguous candidate) is in `claimed` and
    # is skipped: each account gets at most one proposal.
    for acct in accounts:
        is_child = str(acct.get("parent_id")) == str(parent_id)
        is_bellhaven_orphan = (
            acct.get("parent_id") is None
            and "bellhaven" in (acct.get("name") or "").lower()
        )
        if not (is_child or is_bellhaven_orphan):
            continue
        if str(acct.get("id")) == str(parent_id):
            continue
        if str(acct.get("id")) in claimed:
            continue
        if str(acct.get("id")) in preserved_ids:
            continue  # preserved CHOW original: historical, untouchable
        if (acct.get("status") or "").lower() == "inactive":
            continue
        if acct.get("duplicate_of_account"):
            continue
        ar = _money(acct, "outstanding_ar")
        ops = [{"type": "patch", "account_id": acct["id"],
                "fields": {"status": "Inactive",
                           "note_append": f"[{today}] Not listed on the Bellhaven website as of {today}; "
                                          f"outstanding AR ${ar:,.0f}."}}]
        proposals.append({
            "kind": "inactivate", "subject_key": f"account:{acct['id']}", "ops": ops,
            "evidence": {"account": acct,
                         "before": {str(acct["id"]): _snapshot(acct)},
                         "reason": ("Bellhaven-named orphan account (no parent) that no "
                                    "website location matches." if is_bellhaven_orphan and not is_child
                                    else "Under the Bellhaven parent but no website location matches it.")},
        })

    # Invariant: no account is patched by more than one proposal.
    patched_by = {}
    for p in proposals:
        for op in p["ops"]:
            if op["type"] == "patch":
                key = str(op["account_id"])
                assert key not in patched_by, (
                    f"account {key} appears in two proposals: "
                    f"{patched_by.get(key)} and {p['subject_key']}")
                patched_by[key] = p["subject_key"]

    for p in proposals:
        p["id"] = _stable_key(p["kind"], p["subject_key"], p["ops"])
    summary["proposal_counts"] = {}
    for p in proposals:
        summary["proposal_counts"][p["kind"]] = summary["proposal_counts"].get(p["kind"], 0) + 1
    return proposals, summary
