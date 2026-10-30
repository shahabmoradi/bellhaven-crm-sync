# Bellhaven CRM Sync

Keeps a sandbox CRM in sync with the Bellhaven Senior Living website
(34 communities). The website is the source of truth. The pipeline
scrapes every listed location, matches it against all CRM accounts,
and turns the differences into **reviewable proposals**. Nothing is
written to the CRM until a human approves a proposal in the local
review app; approvals execute through one guarded write path.

## Layout

| File | Job |
|---|---|
| `src/scrape.py` | Scrape every community page → `data/locations.json` |
| `src/normalize.py` | Name/address/phone normalization, website→CRM field & care-type maps |
| `src/match.py` | Blocking, weighted scoring, hard vetoes, match tiers |
| `src/proposals.py` | Turn matches into one proposal per location, ordered op list |
| `src/store.py` | SQLite proposal queue (insert-only, supersede on change) |
| `src/executor.py` | The **only** code that writes to the CRM — called solely from the approve route |
| `src/app.py` | Local review app (approve / reject with reason) |
| `src/pipeline.py` | `run`: snapshot CRM → match → propose → store |
| `src/recon.py` | Pre-flight checklist against the live CRM |
| `src/reopen.py` | Audited reopen of a decided proposal |
| `src/review_table.py` | Dump the queue as a readable text table |

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export BELLHAVEN_TOKEN=...        # or put it in .token (gitignored)

.venv/bin/python src/recon.py             # verify token, snapshot, field inventory
.venv/bin/python src/scrape.py            # refresh data/locations.json
.venv/bin/python src/pipeline.py run      # build proposals (read-only vs CRM)
.venv/bin/python src/app.py               # review at http://127.0.0.1:5057
.venv/bin/python src/review_table.py      # text dump of the pending queue
.venv/bin/python tests/test_match.py      # matcher tests
.venv/bin/python tests/test_proposals.py  # proposal / store / executor tests
```

Offline mode: `src/pipeline.py run --fixture tests/fixture_accounts.json`
runs the whole pipeline with no network and no token.

## Safety design

- **Human gate.** The pipeline never writes. Only the review app's
  Approve button calls the executor.
- **Stale check.** Before every write the executor re-reads the
  account and aborts if it changed since the proposal was made.
- **CHOW rule, enforced twice** (proposal time and again at apply
  time): if an account must change parent and `lifetime_revenue > 0`
  **and** `outstanding_ar > 0`, it is never re-parented. The old
  account stays untouched except `chow_current_account` pointing at a
  newly created account under the correct parent.
- **Duplicates before CHOW.** Duplicate clusters are resolved first
  (loser gets `duplicate_of_account` = survivor + status Inactive —
  the API has no merge/delete). No CHOW check runs on losers.
- **Resumable writes.** The API is not transactional, so each
  proposal's ops record per-op progress; a retry resumes where it
  stopped instead of double-creating (covered by a failure-injection
  test).
- **Idempotent re-runs.** Proposals are keyed by
  kind|subject|target-fields. A re-run after approvals diffs against
  the corrected CRM and produces no new proposals for settled items;
  a pending proposal whose target changed is marked *superseded*.
- **Notes are appended, never overwritten**; rejects require a reason.

## Daily run

`.github/workflows/daily.yml` runs scrape + pipeline on a cron
(`0 13 * * *` UTC, ~6 AM Pacific) with the token from repo secrets.
It is read-only against the CRM by design — a human still reviews.

See `WRITEUP.md` for the matching approach, judgment calls, and what
to build next.
