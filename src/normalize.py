"""Normalization helpers for the Bellhaven matcher.

The idea: a website location and a CRM account rarely spell the same
facility the same way. Before we compare anything, both sides are reduced
to a canonical form:

- names lose the brand and generic words, leaving the distinctive core
  ("Bellhaven at Maple Grove" -> "maple grove", "Amberly Manor" -> "amberly")
- street addresses use USPS-style abbreviations and split into a house
  number and a street name ("4390 Darrow Road" -> 4390 + "darrow rd")
- zips are the first 5 digits, zero-padded
- states are 2-letter codes

Everything here is pure and side-effect free so it is easy to unit test.
"""

import re

# Phrases removed from names before tokenizing (multi-word generics first).
_NAME_PHRASES = ["senior living", "assisted living", "memory care", "memory support"]
# Single tokens that carry no identifying power in a facility name.
_NAME_TOKENS = {
    "bellhaven", "the", "at", "of", "llc", "inc", "ltd", "corp", "co",
    "senior", "living", "assisted", "memory", "care", "support",
}

# USPS-style street suffix / direction abbreviations.
_STREET_WORDS = {
    "street": "st", "avenue": "ave", "road": "rd", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl",
    "circle": "cir", "highway": "hwy", "parkway": "pkwy", "trail": "trl",
    "terrace": "ter", "north": "n", "south": "s", "east": "e", "west": "w",
}

# Unit designators we strip from street addresses ("Ste 200", "#4", ...).
_UNIT_RE = re.compile(r"\b(apt|apartment|ste|suite|unit|bldg|fl|floor|rm|room|#)\b.*$", re.I)

_STATE_CODES = {
    "ohio": "OH", "michigan": "MI", "pennsylvania": "PA", "indiana": "IN",
    "california": "CA", "texas": "TX", "florida": "FL", "new york": "NY",
    "illinois": "IL", "georgia": "GA", "arizona": "AZ", "nevada": "NV",
    "oregon": "OR", "washington": "WA", "colorado": "CO",
}


def name_core(name):
    """Reduce a facility name to its distinctive core tokens (a string)."""
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    for phrase in _NAME_PHRASES:
        text = text.replace(phrase, " ")
    tokens = []
    for tok in text.split():
        if tok == "st":
            tok = "saint"  # in names, "St." means Saint, not Street
        if tok not in _NAME_TOKENS:
            tokens.append(tok)
    return " ".join(tokens)


def split_street(address):
    """Split a street address into (house_number, normalized street name).

    Unit/suite part is dropped. Returns (None, text) when no leading
    house number is present.
    """
    text = (address or "").lower()
    text = _UNIT_RE.sub("", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = " ".join(text.split())
    m = re.match(r"^(\d+)\s+(.*)$", text)
    if not m:
        return None, text
    house, street = m.group(1), m.group(2)
    street = " ".join(_STREET_WORDS.get(tok, tok) for tok in street.split())
    return house, street


def zip5(value):
    """First 5 digits of a zip code, zero-padded; '' when absent."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[:5].zfill(5) if digits else ""


def state_code(value):
    """Normalize a state to its 2-letter code ('' when unknown)."""
    text = (value or "").strip().lower()
    if len(text) == 2:
        return text.upper()
    return _STATE_CODES.get(text, "")


def city_key(value):
    return re.sub(r"[^a-z0-9 ]", " ", (value or "").lower()).strip()


def phone_key(value):
    """Last 10 digits of a phone number; '' when absent."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else ""


# The website and the CRM use different care vocabularies. This is the
# explicit mapping (website label -> CRM care_type label).
WEB_TO_CRM_CARE = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
}


def canonical_account(raw):
    """Translate a raw API account record into the canonical shape the
    rest of the system works with (the FIELD_MAP, pinned at recon):

        account_id        -> id            billing_street -> street_address
        billing_city      -> city          billing_zip    -> zip
        billing_state     -> state         care_type      -> care_offerings (list)
        parent_id ''      -> None          duplicate_of_account '' -> None
        chow_current_account '' -> None

    Records already in canonical shape pass through unchanged.
    """
    if "account_id" not in raw and "billing_street" not in raw:
        return raw
    care = raw.get("care_type")
    return {
        "id": raw.get("account_id"),
        "name": raw.get("name"),
        "street_address": raw.get("billing_street"),
        "city": raw.get("billing_city"),
        "state": raw.get("billing_state"),
        "zip": raw.get("billing_zip"),
        "care_offerings": [care] if care else [],
        "parent_id": raw.get("parent_id") or None,
        "parent_name": raw.get("parent_name"),
        "status": raw.get("status"),
        "note": raw.get("note") or "",
        "duplicate_of_account": raw.get("duplicate_of_account") or None,
        "chow_current_account": raw.get("chow_current_account") or None,
        "lifetime_revenue": raw.get("lifetime_revenue"),
        "outstanding_ar": raw.get("outstanding_ar"),
        "phone": raw.get("phone"),
        "updated_at": raw.get("updated_at"),
        "raw": raw,
    }


def normalize_location(loc):
    """Website location -> normalized comparison record."""
    house, street = split_street(loc.get("street_address") or loc.get("address_raw", ""))
    return {
        "kind": "location",
        "name_core": name_core(loc.get("name")),
        "house": house,
        "street": street,
        "city": city_key(loc.get("city")),
        "state": state_code(loc.get("state")),
        "zip": zip5(loc.get("zip")),
        "phone": phone_key(loc.get("phone")),
        "raw": loc,
    }


def account_view(acct):
    """CRM account (raw API dict) -> normalized comparison record.

    The API's exact field spellings are confirmed at recon time; the
    fallbacks below tolerate the common variants (street_address /
    address / street, zip / zip_code / postal_code).
    """
    street_raw = (
        acct.get("street_address") or acct.get("address") or acct.get("street") or ""
    )
    zip_raw = acct.get("zip") or acct.get("zip_code") or acct.get("postal_code") or ""
    house, street = split_street(street_raw)
    return {
        "kind": "account",
        "id": acct.get("id"),
        "name_core": name_core(acct.get("name")),
        "house": house,
        "street": street,
        "city": city_key(acct.get("city")),
        "state": state_code(acct.get("state")),
        "zip": zip5(zip_raw),
        "phone": phone_key(acct.get("phone")),
        "raw": acct,
    }
