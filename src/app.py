"""Review app: a human approves or rejects every proposed change.

    python3 src/app.py            # serves on http://127.0.0.1:5057

Nothing here writes to the CRM except the approve route, which calls
executor.execute() — the single write path in the system. Rejecting
requires a reason. Decided proposals are never re-proposed by the
pipeline (stable ids, see proposals.py).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, request, redirect, url_for, render_template_string, abort

import store
import executor

app = Flask(__name__)

PAGE = """
<!doctype html><title>Bellhaven CRM Review</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem}
 .card{border:1px solid #ccc;border-radius:8px;padding:1rem;margin:1rem 0}
 .badge{background:#444;color:#fff;border-radius:4px;padding:.1rem .5rem;font-size:.8rem}
 .diff td,.diff th{border:1px solid #ddd;padding:.25rem .5rem;text-align:left}
 pre{background:#f6f6f6;padding:.5rem;overflow:auto;font-size:.8rem}
 .applied{color:green}.rejected{color:#b00}.pending{color:#a60}.failed,.stale{color:#b00}
</style>
<h1>Bellhaven CRM — change review</h1>
<p>Counts: {{ counts.by_kind }} · by status: {{ counts.by_status }}</p>
<p>Filter:
 <a href="{{ url_for('index') }}">all</a> ·
 <a href="{{ url_for('index', status='pending') }}">pending</a> ·
 <a href="{{ url_for('index', status='applied') }}">applied</a> ·
 <a href="{{ url_for('index', status='rejected') }}">rejected</a></p>
{% for p in proposals %}
<div class="card">
 <span class="badge">{{ p.kind }}</span>
 <b>{{ p.subject_key }}</b>
 <span class="{{ p.status }}">[{{ p.status }}]</span>
 {% if p.evidence.get('reason') %}<p>{{ p.evidence.reason }}</p>{% endif %}
 {% if p.evidence.get('notes') %}{% for n in p.evidence.notes %}<p>{{ n }}</p>{% endfor %}{% endif %}
 {% if p.evidence.get('location') %}
   <p><b>Website:</b> {{ p.evidence.location.name }} — {{ p.evidence.location.street_address }},
   {{ p.evidence.location.city }}, {{ p.evidence.location.state }} {{ p.evidence.location.zip }}
   · {{ p.evidence.location.care_offerings }}</p>{% endif %}
 {% if p.evidence.get('account') %}
   <p><b>CRM #{{ p.evidence.account.id }}:</b> {{ p.evidence.account.name }} —
   {{ p.evidence.account.get('street_address') or p.evidence.account.get('address') }},
   {{ p.evidence.account.get('city') }}, {{ p.evidence.account.get('state') }}
   {{ p.evidence.account.get('zip') or p.evidence.account.get('zip_code') }}
   · parent {{ p.evidence.account.get('parent_id') }} · status {{ p.evidence.account.get('status') }}
   · revenue {{ p.evidence.financials.lifetime_revenue if p.evidence.get('financials') else '?' }}
   · AR {{ p.evidence.financials.outstanding_ar if p.evidence.get('financials') else '?' }}</p>{% endif %}
 {% if p.evidence.get('score') is not none and p.evidence.get('score') is defined %}
   <p><b>Match score:</b> {{ p.evidence.score }} {{ p.evidence.breakdown }}</p>{% endif %}
 {% if p.evidence.get('diff') %}
   <table class="diff"><tr><th>field</th><th>CRM now</th><th>website</th></tr>
   {% for f, have, want in p.evidence.diff %}<tr><td>{{ f }}</td><td>{{ have }}</td><td>{{ want }}</td></tr>{% endfor %}
   </table>{% endif %}
 {% if p.evidence.get('candidates') %}
   {% for c in p.evidence.candidates %}<p>Candidate #{{ c.account_id }}: score {{ c.score }} {{ c.breakdown }}</p>{% endfor %}{% endif %}
 <p><b>Operations if approved:</b></p><pre>{{ p.ops | tojson(indent=2) }}</pre>
 {% if p.status in ('pending', 'failed') and p.ops %}
 <form method="post" action="{{ url_for('approve', proposal_id=p.id) }}" style="display:inline">
   <button type="submit">{{ 'Retry (resumes)' if p.status == 'failed' else 'Approve & apply' }}</button></form>
 {% endif %}
 {% if p.status == 'pending' %}
 <form method="post" action="{{ url_for('reject', proposal_id=p.id) }}" style="display:inline">
   <input name="reason" placeholder="reason (required)" required>
   <button type="submit">Reject</button></form>
 {% endif %}
 {% if p.result %}<pre>{{ p.result | tojson(indent=2) }}</pre>{% endif %}
</div>
{% endfor %}
"""


@app.route("/")
def index():
    conn = store.connect()
    status = request.args.get("status")
    return render_template_string(
        PAGE, proposals=store.list_proposals(conn, status), counts=store.counts(conn))


@app.route("/proposal/<proposal_id>/approve", methods=["POST"])
def approve(proposal_id):
    conn = store.connect()
    proposal = store.get(conn, proposal_id)
    # 'failed' proposals may be retried: the executor resumes from the
    # persisted per-op progress instead of starting over.
    if not proposal or proposal["status"] not in ("pending", "failed"):
        abort(404)
    try:
        result = executor.execute(
            proposal,
            save_progress=lambda progress: store.save_result(conn, proposal_id, progress))
        store.set_status(conn, proposal_id, "applied", result=result)
    except executor.StaleError as exc:
        store.set_status(conn, proposal_id, "stale", reason=str(exc))
    except Exception as exc:  # progress already saved per-op; safe to retry
        store.set_status(conn, proposal_id, "failed", reason=str(exc))
    return redirect(url_for("index"))


@app.route("/proposal/<proposal_id>/reject", methods=["POST"])
def reject(proposal_id):
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        abort(400, "a reason is required to reject")
    conn = store.connect()
    proposal = store.get(conn, proposal_id)
    if not proposal or proposal["status"] != "pending":
        abort(404)
    store.set_status(conn, proposal_id, "rejected", reason=reason)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057)
