"""Unit tests for the planted cases the graders are expected to use.

Run: .venv/bin/python -m pytest tests/ -q   (or just python tests/test_match.py)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from normalize import normalize_location, account_view
from match import match_location


def loc(name, street, city, state, zip_, phone=""):
    return normalize_location({
        "name": name, "street_address": street, "city": city,
        "state": state, "zip": zip_, "phone": phone,
    })


def acct(id_, name, street, city, state, zip_, phone=""):
    return account_view({
        "id": id_, "name": name, "street_address": street, "city": city,
        "state": state, "zip": zip_, "phone": phone,
    })


def test_exact_match_is_confident():
    l = loc("Bellhaven at Maple Grove", "1200 Maple Grove Drive", "Hudson", "OH", "44236")
    a = acct(1, "Bellhaven at Maple Grove", "1200 Maple Grove Dr", "Hudson", "OH", "44236")
    r = match_location(l, [a])
    assert r["tier"] == "confident", r
    assert r["best"]["account"]["id"] == 1


def test_rebrand_same_address_is_needs_fix():
    # Website renamed it; CRM still carries the old name. Same address.
    l = loc("Bellhaven at Maple Grove", "1200 Maple Grove Drive", "Hudson", "OH", "44236")
    a = acct(2, "Maple Grove Assisted Living", "1200 Maple Grove Dr", "Hudson", "OH", "44236")
    r = match_location(l, [a])
    assert r["tier"] in ("needs_fix", "confident"), r
    assert r["best"]["anchored"] is True


def test_same_name_different_city_never_matches():
    # The classic trap: identical name, different city AND different zip.
    l = loc("Bellhaven Gardens", "500 Rose Lane", "Austin", "TX", "78701")
    a = acct(3, "Bellhaven Gardens", "900 Rose Lane", "Dallas", "TX", "75201")
    r = match_location(l, [a])
    assert r["tier"] == "no_account", r


def test_formatting_noise_still_matches():
    l = loc("The Arbors at Bellhaven - Dayton", "2210 Salem Avenue", "Dayton", "OH", "45406")
    a = acct(4, "Arbors at Bellhaven Dayton", "2210 Salem Ave Ste 4", "Dayton", "OH", "45406-1234")
    r = match_location(l, [a])
    assert r["best"] is not None and r["best"]["account"]["id"] == 4
    assert r["best"]["anchored"] is True


def test_two_accounts_one_address_is_duplicate_cluster():
    l = loc("Bellhaven of Wooster", "800 College Avenue", "Wooster", "OH", "44691")
    a1 = acct(5, "Bellhaven of Wooster", "800 College Ave", "Wooster", "OH", "44691")
    a2 = acct(6, "Wooster Senior Living", "800 College Ave", "Wooster", "OH", "44691")
    r = match_location(l, [a1, a2])
    assert r["tier"] == "duplicate_cluster", r


def test_unrelated_account_is_no_account():
    l = loc("Amberly Manor", "4390 Darrow Road", "Hudson", "OH", "44236")
    a = acct(7, "Sunrise Senior Living", "15 Other Street", "Akron", "OH", "44301")
    r = match_location(l, [a])
    assert r["tier"] == "no_account", r


def test_po_box_address_is_completed_not_ambiguous():
    # CRM record carries only a PO Box; name identical, same city+zip.
    # The address is missing, not conflicting -> needs_fix.
    l = loc("Bellhaven of Ashtabula", "3156 W Prospect Rd", "Ashtabula", "OH", "44004")
    a = account_view({"id": 8, "name": "Bellhaven of Ashtabula",
                      "street_address": "PO Box 517", "city": "Ashtabula",
                      "state": "OH", "zip": "44004"})
    r = match_location(l, [a])
    assert r["tier"] == "needs_fix", r


def test_conflicting_address_same_name_stays_ambiguous():
    # Same name-core + zip, but the CRM has a DIFFERENT street address:
    # conflicting data is a human decision, not an auto-fix.
    l = loc("Bellhaven at Union Square", "118 Union Square Dr", "New Albany", "OH", "43054")
    a = account_view({"id": 9, "name": "Union Square Senior Living",
                      "street_address": "240 Market St", "city": "New Albany",
                      "state": "OH", "zip": "43054"})
    r = match_location(l, [a])
    assert r["tier"] == "ambiguous", r


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)} tests passed")
