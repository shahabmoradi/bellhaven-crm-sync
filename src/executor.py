"""Executor: the ONLY module allowed to write to the CRM.

It is called from exactly one place — the review app's approve route.
The nightly pipeline never imports it.

Safety properties:
1. Stale check: each account a patch touches is re-fetched first,
   and EVERY field in its proposal-time snapshot must still hold —
   not only the fields the patch changes. The snapshot covers every
   touched account (survivor, duplicate losers, CHOW source) across
   name, parent, status, duplicate/CHOW pointers, address, care, and
   the money fields. The unchanged fields are the identity evidence:
   if the address that anchored the match, or the revenue/AR that
   decided the CHOW branch, has moved, the proposal is stale and
   nothing writes.
   The CHOW gate is also re-run at apply time: for a split proposal
   the whole gate (parent, revenue, AR, chow_current_account), and
   for ANY direct re-parent, if the account has since gained
   revenue > 0 AND AR > 0 the branch would flip to a split, so the
   proposal is stale and nothing writes.
2. Resumable: the API is not transactional, so after every single op
   the progress (ops completed + any created id) is persisted through
   the save_progress callback. A retry after a partial failure skips
   finished ops and reuses the stored created id instead of creating
   a duplicate account.
3. Create-guard: before any create, the CRM is searched under the
   Bellhaven parent for the same normalized address + zip. A hit
   means the account already exists (e.g. from a crashed earlier
   run): it is linked, not duplicated.
4. note_append is composed against the FRESH record at apply time,
   so existing notes are preserved (appended to, never overwritten).
5. Every write attempt is appended to data/write_log.jsonl, and each
   patch is verified by re-fetching the account afterwards.
"""

import crm
from normalize import account_view, canonical_account, WEB_TO_CRM_CARE

BEFORE_FIELDS = ("name", "parent_id", "status", "duplicate_of_account",
                 "chow_current_account", "street_address", "city", "state",
                 "zip", "care_offerings", "lifetime_revenue", "outstanding_ar")

_MONEY_FIELDS = ("lifetime_revenue", "outstanding_ar")


def _num(value):
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _field_eq(field, a, b):
    """Before-value comparison. care_offerings compares as a set of
    labels (order and representation differ between snapshots);
    money compares numerically; everything else compares as strings,
    with None and '' equal."""
    if field == "care_offerings":
        def labels(v):
            if v is None:
                return frozenset()
            if isinstance(v, str):
                v = v.replace(";", ",").split(",")
            return frozenset(str(x).strip().lower() for x in v if str(x).strip())
        return labels(a) == labels(b)
    if field in _MONEY_FIELDS:
        return _num(a) == _num(b)
    return str(a or "") == str(b or "")

# Canonical field name -> API field name (the FIELD_MAP, write side).
WRITE_MAP = {
    "street_address": "billing_street",
    "city": "billing_city",
    "state": "billing_state",
    "zip": "billing_zip",
}


def translate_fields(fields):
    """Translate canonical op fields into API field names/values.
    care_offerings (a website-vocabulary list) becomes care_type
    (a single CRM label, mapped; the first listed offering wins
    when a facility offers several)."""
    out = {}
    for key, value in fields.items():
        if key == "care_offerings":
            labels = [WEB_TO_CRM_CARE.get(o, o) for o in (value or [])]
            out["care_type"] = labels[0] if labels else None
        else:
            out[WRITE_MAP.get(key, key)] = value
    return out


class StaleError(Exception):
    pass


def _money(record, key):
    return _num(record.get(key))


def _before_values(proposal):
    """account_id -> {field: value at proposal time} from the evidence.

    The builder snapshots every account any op touches into
    evidence["before"]; the snapshot is used AS STORED — the stale
    check compares exactly the fields the snapshot carries, so a
    proposal built before a field was added to the snapshot is
    checked on the fields it has, and newer proposals on all of
    them. The single-account evidence["account"] shape is kept as a
    fallback for older proposals."""
    ev = proposal.get("evidence") or {}
    before = {}
    for aid, snap in (ev.get("before") or {}).items():
        before[str(aid)] = dict(snap)
    acct = ev.get("account")
    if acct and acct.get("id") is not None and str(acct["id"]) not in before:
        before[str(acct["id"])] = {f: acct.get(f) for f in BEFORE_FIELDS}
    return before


def _guard_lookup(op):
    """Create-guard: an existing account under the same parent with
    the same house number, street, and zip means 'already created'."""
    guard = op.get("guard")
    if not guard:
        return None
    for acct in crm.fetch_all_accounts():
        if str(acct.get("parent_id")) != str(guard.get("parent_id")):
            continue
        view = account_view(canonical_account(acct))
        if (view["house"] == guard.get("house") and view["street"] == guard.get("street")
                and view["zip"] == guard.get("zip")):
            # fetch_all_accounts returns RAW API records, whose id
            # field is account_id — reading "id" here returned None
            # and silently disabled the guard.
            return acct.get("account_id") or acct.get("id")
    return None


def execute(proposal, dry_run=False, save_progress=None):
    """Run a proposal's ops in order, resuming from saved progress.

    Returns a result dict. Raises StaleError before any write if the
    world has moved since the proposal was built.
    """
    ops = proposal["ops"]
    before = _before_values(proposal)
    prior = proposal.get("result") or {}
    done_ops = int(prior.get("done_ops", 0))
    created_id = prior.get("created_id")
    log = list(prior.get("log", []))

    fresh = {}

    def fresh_account(aid):
        if aid not in fresh:
            fresh[aid] = canonical_account(crm.fetch_account(aid))
        return fresh[aid]

    # --- Apply-time re-evaluation -----------------------------------
    # Ops already completed by an earlier attempt are skipped: their
    # own writes are exactly why the record no longer matches the
    # proposal-time "before" values, and re-checking them would make
    # every resumable proposal un-resumable.
    for idx, op in enumerate(ops):
        if idx < done_ops:
            continue
        if op["type"] == "patch":
            expected = before.get(str(op["account_id"]), {})
            record = fresh_account(op["account_id"])
            # EVERY snapshotted field is compared — not only the ones
            # this op changes. The unchanged fields are the proposal's
            # identity evidence (the address that anchored the match,
            # the money that decided the CHOW branch): if any of them
            # moved since the proposal was built, the proposal may be
            # acting on a different record or the wrong branch, and a
            # human re-run — not a write — is the right outcome.
            for field, want in expected.items():
                if not _field_eq(field, record.get(field), want):
                    raise StaleError(
                        f"account {op['account_id']} field {field}: was "
                        f"{want!r}, now {record.get(field)!r}")
    # CHOW gate re-check for DIRECT re-parents (any proposal kind):
    # a patch that moves an account between two real parents is only
    # the right branch while revenue or AR is zero. If both have
    # become positive since the proposal was built, the SOP branch
    # flips to a split — stop and let the pipeline re-propose.
    # Orphan assignments (current parent null) are exempt: assigning
    # a parent is not a move.
    for idx, op in enumerate(ops):
        if idx < done_ops:
            continue
        if op["type"] == "patch" and "parent_id" in (op.get("fields") or {}):
            record = fresh_account(op["account_id"])
            current = record.get("parent_id")
            target = op["fields"]["parent_id"]
            if current is not None and str(current) != str(target):
                if (_money(record, "lifetime_revenue") > 0
                        and _money(record, "outstanding_ar") > 0):
                    raise StaleError(
                        f"account {op['account_id']}: CHOW gate is now met "
                        f"(revenue and AR both > 0) — a direct re-parent is "
                        f"no longer the right branch; re-run the pipeline "
                        f"so this becomes a split")
    if proposal.get("kind") == "chow_split":
        create_op = next((o for o in ops if o["type"] == "create"), None)
        if create_op is not None:
            survivor = fresh_account(create_op["link_from"])
            parent = create_op["fields"].get("parent_id")
            if str(survivor.get("parent_id")) == str(parent):
                raise StaleError("CHOW survivor is already under the Bellhaven parent")
            if survivor.get("chow_current_account"):
                raise StaleError("CHOW survivor already has chow_current_account set")
            if not (_money(survivor, "lifetime_revenue") > 0
                    and _money(survivor, "outstanding_ar") > 0):
                raise StaleError("CHOW gate no longer holds at apply time "
                                 "(revenue/AR changed)")

    # --- Execute, resumably -----------------------------------------
    for idx, op in enumerate(ops):
        if idx < done_ops:
            continue
        if op["type"] == "create":
            fields = translate_fields(dict(op["fields"]))
            if dry_run:
                created_id = created_id if created_id is not None else -1
                log.append({"op": "create", "fields": fields, "dry_run": True})
            else:
                hit = _guard_lookup(op)
                if hit is not None:
                    created_id = hit
                    crm.write_log({"proposal": proposal["id"], "op": "create_guard_hit",
                                    "existing_id": hit})
                    log.append({"op": "create", "created_id": hit, "guard_hit": True})
                else:
                    created = crm.create_account(fields)
                    created_id = created.get("id") or created.get("account_id")
                    crm.write_log({"proposal": proposal["id"], "op": "create",
                                    "fields": fields, "created_id": created_id})
                    log.append({"op": "create", "created_id": created_id})
        elif op["type"] == "patch":
            aid = op["account_id"]
            fields = {}
            for key, value in (op.get("fields") or {}).items():
                if value == "$created_id":
                    value = created_id
                if key == "note_append":
                    existing = (fresh_account(aid) or {}).get("note") or ""
                    fields["note"] = (existing + "\n" + value).strip() if existing else value
                else:
                    fields[key] = value
            fields = translate_fields(fields)
            if dry_run:
                log.append({"op": "patch", "account_id": aid, "fields": fields, "dry_run": True})
            else:
                crm.patch_account(aid, fields)
                crm.write_log({"proposal": proposal["id"], "op": "patch",
                               "account_id": aid, "fields": fields})
                check = crm.fetch_account(aid)
                for key, value in fields.items():
                    if key != "note" and str(check.get(key)) != str(value):
                        raise RuntimeError(
                            f"verify failed on account {aid}: {key} is "
                            f"{check.get(key)!r}, want {value!r}")
                log.append({"op": "patch", "account_id": aid, "fields": fields, "verified": True})
        else:
            raise ValueError(f"unknown op type: {op['type']}")

        done_ops = idx + 1
        if save_progress is not None:
            save_progress({"done_ops": done_ops, "created_id": created_id,
                           "log": log, "dry_run": dry_run})

    return {"done_ops": done_ops, "created_id": created_id, "log": log, "dry_run": dry_run}
