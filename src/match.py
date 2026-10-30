"""Matching: pair each website location with the CRM account that is
most likely the same facility.

Design (per the build spec):
- The ADDRESS is the primary key; the name is supporting evidence.
- Candidate pairs come from blocking (shared zip, shared city+state, or
  shared house number) so we never compare everything to everything.
- Each candidate pair gets a 0-100 score from weighted signals.
- Hard vetoes kill a pair outright no matter how good the name looks:
  different states, or different house number AND different zip.
- Each location is then tiered: confident / needs_fix candidate /
  ambiguous / no_account. Parent, status, duplicate and CHOW handling
  happen later, in the proposal builder, where the full CRM picture
  is available.
"""

from rapidfuzz.fuzz import token_set_ratio

# --- Scoring weights -------------------------------------------------
PTS_STREET_STRONG = 40   # house number equal, street similarity >= 90
PTS_STREET_WEAK = 25     # house number equal, street similarity 75-89
PTS_ZIP = 15             # zip5 equal
PTS_CITY_STATE = 10      # city and state equal
NAME_WEIGHT = 0.30       # name-core similarity contributes up to 30
PTS_PHONE = 5            # phone equal. Small on purpose: over half the
                         # website phone numbers have area codes from
                         # the wrong state, so phone is weak evidence.

TIER_CONFIDENT = 85      # score >= 85 and name core >= 85
TIER_FLOOR = 50          # best score < 50 -> treat as no CRM account
AMBIGUITY_GAP = 10       # top two candidates this close -> ambiguous
DUP_CLUSTER_MIN = 70     # 2+ anchored accounts above this for one
                         # location = duplicate cluster, not a tie


def _street_ratio(a, b):
    if not a or not b:
        return 0.0
    return token_set_ratio(a, b)


def _name_ratio(a, b):
    if not a or not b:
        return 0.0
    return token_set_ratio(a, b)


def score_pair(loc, acct):
    """Score one normalized location against one normalized account.

    Returns (score, breakdown, veto_reason). veto_reason is None when
    the pair survives; score is 0 when vetoed.
    """
    # Hard vetoes first.
    if loc["state"] and acct["state"] and loc["state"] != acct["state"]:
        return 0, {}, "different state"
    if (
        loc["house"] and acct["house"] and loc["house"] != acct["house"]
        and loc["zip"] and acct["zip"] and loc["zip"] != acct["zip"]
    ):
        return 0, {}, "different house number and different zip"

    street_ratio = _street_ratio(loc["street"], acct["street"])
    breakdown = {}
    score = 0.0
    if loc["house"] and acct["house"] and loc["house"] == acct["house"]:
        if street_ratio >= 90:
            breakdown["street"] = PTS_STREET_STRONG
        elif street_ratio >= 75:
            breakdown["street"] = PTS_STREET_WEAK
    score += breakdown.get("street", 0)

    if loc["zip"] and acct["zip"] and loc["zip"] == acct["zip"]:
        breakdown["zip"] = PTS_ZIP
        score += PTS_ZIP

    if loc["city"] and loc["city"] == acct["city"] and loc["state"] == acct["state"]:
        breakdown["city_state"] = PTS_CITY_STATE
        score += PTS_CITY_STATE

    name_ratio = _name_ratio(loc["name_core"], acct["name_core"])
    breakdown["name"] = round(name_ratio * NAME_WEIGHT, 1)
    score += breakdown["name"]

    if loc["phone"] and acct["phone"] and loc["phone"] == acct["phone"]:
        breakdown["phone"] = PTS_PHONE
        score += PTS_PHONE

    return min(round(score, 1), 100), breakdown, None


def has_address_anchor(loc, acct):
    """The strong anchor: same house number, near-identical street, same zip."""
    return bool(
        loc["house"] and acct["house"] and loc["house"] == acct["house"]
        and _street_ratio(loc["street"], acct["street"]) >= 90
        and loc["zip"] and acct["zip"] and loc["zip"] == acct["zip"]
    )


def is_candidate(loc, acct):
    """Blocking: could this pair possibly be the same facility?"""
    if loc["zip"] and acct["zip"] and loc["zip"] == acct["zip"]:
        return True
    if loc["city"] and loc["city"] == acct["city"] and loc["state"] == acct["state"]:
        return True
    if loc["house"] and acct["house"] and loc["house"] == acct["house"]:
        return True
    return False


def match_location(loc, accounts):
    """Rank all candidate accounts for one location and assign a tier.

    Returns a dict with the ranked candidates (best first), the chosen
    tier, and the score details a reviewer needs as evidence.
    """
    scored = []
    for acct in accounts:
        if not is_candidate(loc, acct):
            continue
        score, breakdown, veto = score_pair(loc, acct)
        if veto:
            continue
        scored.append({
            "account": acct,
            "score": score,
            "breakdown": breakdown,
            "name_ratio": _name_ratio(loc["name_core"], acct["name_core"]),
            "anchored": has_address_anchor(loc, acct),
        })
    scored.sort(key=lambda c: c["score"], reverse=True)

    if not scored or scored[0]["score"] < TIER_FLOOR:
        tier = "no_account"
    else:
        best = scored[0]
        runner_up = scored[1] if len(scored) > 1 else None
        anchored_cluster = [c for c in scored if c["anchored"] and c["score"] >= DUP_CLUSTER_MIN]
        if len(anchored_cluster) >= 2:
            tier = "duplicate_cluster"
        elif (best["name_ratio"] >= 95 and not best["account"]["house"]
              and loc["zip"] and loc["zip"] == best["account"]["zip"]
              and loc["city"] == best["account"]["city"]):
            # Identical name, same city + zip, but the CRM record has
            # NO street address (e.g. only a PO Box): the address is
            # missing, not conflicting. Complete it from the website.
            tier = "needs_fix"
        elif runner_up and best["score"] - runner_up["score"] <= AMBIGUITY_GAP and not best["anchored"]:
            tier = "ambiguous"
        elif best["score"] >= TIER_CONFIDENT and best["name_ratio"] >= 85:
            tier = "confident"
        elif best["anchored"]:
            tier = "needs_fix"
        elif best["score"] >= TIER_FLOOR:
            tier = "ambiguous"
        else:
            tier = "no_account"

    return {
        "location": loc,
        "tier": tier,
        "candidates": scored,
        "best": scored[0] if scored else None,
    }
