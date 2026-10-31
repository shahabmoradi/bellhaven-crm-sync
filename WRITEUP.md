# Writeup — Bellhaven CRM sync

**Matching approach.** The website is the source of truth. All 35
communities were scraped — the directory lists 34, but the home page
links a 35th (Bellhaven Meadows of Findlay, announced as "new this
year") that never appears in the directory; a listing-only crawl
misses it, so discovery also scans the home and About pages. The 35
were then matched against *all* 121 CRM accounts (not just
Bellhaven's children — several Bellhaven facilities sit under other
operators). Matching blocks on zip, city+state, and
house number, then scores: street-address anchor 40 (25 for a partial
anchor), zip 15, city+state 10, name similarity up to 30, phone +5.
Two hard vetoes: different state, or different house number *and*
different zip. Address is the primary key because names lie — three
facilities still carry pre-acquisition names (e.g. "Cedar Trail of
Zanesville"), and one exact-name account (Amberly Manor) is a decoy
in Colorado that the state veto correctly rejects, so the Ohio
facility was created fresh. One case was genuinely ambiguous
(Union Square — same name/city, completely different street). It
was rejected in review with the reason
recorded — different street address; cannot prove it is the same
facility — and the rejection is stored as a confirmed non-match, so
the next run stopped re-proposing the look-alike and instead
proposed creating the missing Bellhaven account at the website
address. The other operator's account was never touched. One
proposal from the first (34-location) run — inactivate Findlay —
was caught as wrong once the 35th page was found, and was rejected
in review with the reason recorded; its replacement simply assigns
the parentless Findlay account to the Bellhaven parent. Final
tally for the 35 locations: 12 already matched cleanly; 19 needed a
fix (5 of them duplicate clusters, 2 of them CHOW splits); 4 needed
a new account. Separately, 3 accounts under the Bellhaven parent
are no longer on the website and were inactivated. Addresses are
compared on their USPS-normalized form — "Street" vs "St" and
"Northwest" vs "NW" are the same address; only substantive
corrections (a wrong zip, a PO Box where a real street exists, a
stale name) are ever proposed. An earlier draft proposed 12
formatting-only syncs; normalized comparison retired them all.
After the last approval was applied, a full re-run of the pipeline
proposed zero changes — all 35 locations classify as confident
matches — which is the idempotency the daily run depends on. The
first post-execution re-run did catch one last bug: Kettering's
already-resolved duplicate losers were re-proposed on their own,
because their survivor-side changes had already landed and changed
the proposal's shape; the builder now treats an account already
stamped as a duplicate of the survivor and Inactive as resolved,
with a regression test.

**Rules that shaped the output.** Duplicates resolve before any
parent change; losers get `duplicate_of_account` + Inactive (no merge
exists in the API). The CHOW rule is applied strictly as written —
revenue > 0 **and** AR > 0: Marietta ($51,250 / $3,800) and Tiffin
($84,000 / $12,400) were split, not moved; Lima (AR $0) was
re-parented directly. The gate is re-checked inside the executor at
apply time — for splits, and for any direct re-parent whose numbers
have since crossed the line — so a stale proposal cannot take the
wrong branch. A preserved account is untouchable afterwards: it is
excluded from matching on later runs, so a re-run can never come
back and stamp it a "duplicate" of the account its own split
created. The gate also applies to duplicate *losers*: a losing copy
that sits under another operator with revenue and AR is that
operator's billing record for the same facility, not a duplicate —
its only change is `chow_current_account` pointing at the survivor.
(All seven losers in this dataset are $0/$0, verified against three
separate exports, so the rule changes no outcome here; it is in the
code, with a test, so the behavior is right by construction.) Judgment calls, stated plainly:
(1) the SOP governs moving *between* parents; null → Bellhaven is
assignment, not a move, so a parentless Bellhaven account is
assigned the parent directly — or inactivated if it is not on the
website; (2) phone got only +5: 38% agreement on address-anchored
pairs, so phone is corroboration only, never decisive;
(3) Sandusky: Inactive because no longer operated; its $5,200 AR is
recorded in the note so collections retains visibility. Alternative
considered: Needs Review; (4) a rejected ambiguous match is recorded
as a confirmed non-match for that location/account pair, and the
location then falls through to a create (Union Square) — the
alternative, approving it, would have flagged the *other operator's*
account Needs Review and still left the website location with no
account of ours; (5) a CRM care label that is one of the website's
offerings counts as in sync (no churn over label granularity).

**How AI tools were used.** Freely, as the exercise intends. I
directed an AI assistant (Muse) as designer, builder, and tester:
it wrote the scraper, matcher, proposal engine, and review app, and
generated the tests, including a failure-injection test proving a
retried write creates exactly one account. I consulted a second AI
(Claude) as a design reviewer before building and again after; its
corrections — snapshot-first recon, duplicates before CHOW,
resumable writes, re-checking the CHOW gate at apply time — are in
the final design. After the first review session, I had a third AI
(Perplexity, running a GPT model) independently audit the work
against these directions: it re-crawled the website, diffed a fresh
CRM export against the pre-work snapshot field by field (exactly
the three approved demo changes, nothing else), and reproduced
four real defects the first two reviewers missed — the worst being
that a re-run would have marked Marietta's preserved account a
duplicate of its own split. All four were fixed and covered by new
named tests before the remaining proposals were approved. The
remaining queue (25 items) was then reviewed group by group before
execution; that review added an executor guard that refuses any
field outside the CRM's real schema, and a sweep that retires a
stale pending proposal when a newer run re-proposes the same
account under a different subject. All CRM writes went through
approve/reject decisions in the review app; nothing was ever
written by the pipeline itself.

**What I would build next.** A reviewer UI showing a map pin and a
street-photo thumbnail per proposal (address disputes become
one-glance decisions); confidence-based auto-approval for the
lowest-risk class — single-field, non-money, non-parent fixes —
keeping humans on money and parent changes; and a weekly digest of
Needs Review items so ambiguous cases age visibly instead of
silently.

**Time spent:** about 4 hours
