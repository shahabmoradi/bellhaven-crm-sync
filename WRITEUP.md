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
in Colorado that the state veto correctly rejects. Results: 24 fixes,
5 duplicate clusters, 2 CHOW splits, 3 creates, 3 inactivations, and
1 genuinely ambiguous case (Union Square — same name/city, completely
different street) which is flagged Needs Review rather than guessed.
One proposal from the first (34-location) run — inactivate Findlay —
was caught as wrong once the 35th page was found, and was rejected in
review with the reason recorded; its replacement simply assigns the
parentless Findlay account to the Bellhaven parent.

**Rules that shaped the output.** Duplicates resolve before any
parent change; losers get `duplicate_of_account` + Inactive (no merge
exists in the API). The CHOW rule is applied strictly as written —
revenue > 0 **and** AR > 0 — on the surviving account only: Marietta
($51,250 / $3,800) and Tiffin ($84,000 / $12,400) were split, not
moved; Lima (AR $0) was re-parented directly. Judgment calls, stated
plainly: (1) a Bellhaven-named account with no parent counts as
Bellhaven scope — assigned the parent, or inactivated if it is not on
the website (Findlay); (2) phone got only +5 — it agreed on just 38%
of address-anchored pairs, so it corroborates, never decides;
(3) Sandusky was inactivated despite $5,200 outstanding AR, with a
note flagging the AR — it is gone from the website, and the note
preserves the money trail; (4) approving the ambiguous proposal flags
Needs Review naming both candidates instead of forcing a match;
(5) a CRM care label that is one of the website's offerings counts as
in sync (no churn over label granularity).

**How AI tools were used.** Freely, as the exercise intends. I
directed an AI assistant (Muse) as designer, builder, and tester:
it wrote the scraper, matcher, proposal engine, and review app, and
generated the tests, including a failure-injection test proving a
retried write creates exactly one account. I consulted a second AI
(Claude) as a design reviewer before building and again after; its
corrections — snapshot-first recon, duplicates before CHOW,
resumable writes, re-checking the CHOW gate at apply time — are in
the final design. Every proposal was then hand-checked against the
raw data before the review session, and all CRM writes went through
my own approve/reject clicks in the review app.

**What I would build next.** A reviewer UI showing a map pin and a
street-photo thumbnail per proposal (address disputes become
one-glance decisions); confidence-based auto-approval for pure
string-format syncs below a risk threshold, keeping humans on money
and parent changes; and a weekly digest of Needs Review items so
ambiguous cases age visibly instead of silently.

**Time spent:** TIME_SPENT_TO_FINALIZE
