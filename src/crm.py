"""Client for the Bellhaven CRM sandbox API.

Two halves, deliberately:
- The read functions (fetch everything) are used by the pipeline.
- The write functions (create / patch) are imported ONLY by
  executor.py, which is called ONLY from the review app's approve
  route. That is how "nothing writes without approval" is enforced:
  there is exactly one code path that can change the CRM.

The token comes from the BELLHAVEN_TOKEN environment variable or a
.token file next to the project root. It is never printed or logged.
"""

import json
import os
import time
from pathlib import Path

import requests

BASE = "https://analyst-assessment-production.up.railway.app/api/v1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _token():
    tok = os.environ.get("BELLHAVEN_TOKEN", "").strip()
    if tok:
        return tok
    token_file = PROJECT_ROOT / ".token"
    if token_file.exists():
        return token_file.read_text().strip()
    raise RuntimeError(
        "No CRM token found. Set BELLHAVEN_TOKEN or put the token in "
        f"{token_file} (the token is shown on the assessment workspace page)."
    )


def _headers():
    return {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}


def _get(path, params=None):
    resp = requests.get(f"{BASE}{path}", headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------- reads

def fetch_all_accounts(page_size=50):
    """Pull every account, following pagination. Returns raw dicts.
    The API envelope is {"data": [...], "page", "page_size", "total"}
    and the server caps page_size at 50."""
    accounts = []
    page = 1
    while True:
        data = _get("/accounts", params={"page": page, "page_size": page_size})
        if isinstance(data, list):
            batch, total = data, None
        else:
            batch = data.get("data") or data.get("items") or data.get("accounts") or []
            total = data.get("total")
        accounts.extend(batch)
        if not batch or (total is not None and len(accounts) >= total) or len(batch) < page_size:
            return accounts
        page += 1


def fetch_account(account_id):
    return _get(f"/accounts/{account_id}")


def whoami():
    return _get("/me")


# --------------------------------------------------------------- writes
# Imported by executor.py only.

def create_account(fields):
    resp = requests.post(
        f"{BASE}/accounts", headers=_headers(), json=fields, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def patch_account(account_id, fields):
    resp = requests.patch(
        f"{BASE}/accounts/{account_id}", headers=_headers(), json=fields, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


# ------------------------------------------------------------ key names
# The API's exact field spellings are learned from the records
# themselves at recon time, so patches reuse whatever keys the
# existing record already carries instead of guessing.

def pick_key(record, *candidates, default=None):
    for key in candidates:
        if key in record:
            return key
    return default


def write_log(entry, path=None):
    """Append one JSON line per write attempt (audit trail)."""
    path = path or (PROJECT_ROOT / "data" / "write_log.jsonl")
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
