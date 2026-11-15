# ruff: noqa: E501
"""The procurement process around one number, the purchase request (PR): who was asked, who answered, how each
quotation ranks, who won, whether a purchase order is needed, and how far the winner's payment is.

  Preferred vendors   Every supplier that sends a quotation is kept as a preferred vendor with its own number
                      (PV-0001 ...): who it is, what it quoted for, how often, how complete its papers were. The next
                      request that fits is sent to the preferred vendors first; the web is searched only for what is
                      missing to five (sourcing.find_vendors).
  Ranking             Every quotation of a PR, best first: every specification before price, price among those that
                      meet them, then how complete the vendor's administration is (legal name, registration, tax id,
                      contact, address, bank account, the quotation document, terms). With the conclusion: a winner
                      was decided, or a person must look, and why.
  Vendor list         The winners. A purchase above the PO threshold (Procurement settings) gets a draft purchase
                      order document; one below it is a direct purchase, announced to the winner with the same
                      reference. Either way the reference is the PO number the invoice must quote, so the winner's
                      invoice lands in Payment → Intake against the same PR and PO. The row shows each step: announced,
                      details, invoice, payment.
"""
from __future__ import annotations

import html
import re
from datetime import UTC, datetime

KIND, SETTINGS, SETTINGS_ID = "preferred_vendor", "procurement_settings", "settings"
MIN_VENDORS = 5  # a request is sent to at least this many relevant vendors
DEFAULT_SETTINGS = {"po_threshold_minor": 100_000, "po_threshold_currency": "USD"}
# what a vendor's administration consists of, and how it is shown when missing
ADMIN = [("legal_name", "legal name"), ("registration_no", "company registration number"), ("tax_id", "tax id"), ("contact_person", "contact person"),
         ("email", "email"), ("phone", "phone"), ("street_address", "address"), ("country_code", "country")]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _key(vendor: dict) -> str:
    """One vendor, one record: its email address, else its name."""
    return (vendor.get("email") or "").strip().lower() or re.sub(r"[^a-z0-9]+", "-", (vendor.get("legal_name") or vendor.get("name") or "").lower()).strip("-")


def admin(q: dict) -> dict:
    """How complete a quotation's administration is: {"have", "of", "missing"}."""
    v = q.get("vendor") or {}
    missing = [label for field, label in ADMIN if not str(v.get(field) or "").strip()]
    if not (v.get("account_name") and (v.get("account_number") or v.get("iban"))):
        missing.append("bank account")
    if not q.get("file"):
        missing.append("quotation document")
    if not str(q.get("payment_terms") or "").strip():
        missing.append("payment terms")
    of = len(ADMIN) + 3
    return {"have": of - len(missing), "of": of, "missing": missing}


# ---- preferred vendors ----

def preferred(org) -> list[dict]:
    return sorted(org.list(KIND), key=lambda p: p["_id"])


def record_preferred(org, q: dict, pr: dict) -> dict:
    """A quotation came in: its vendor is a preferred vendor from now on (or its record is brought up to date)."""
    key = _key(q["vendor"])
    cur = next((p for p in org.list(KIND) if p["key"] == key), None)
    if cur is None:
        taken = {p["_id"] for p in org.list(KIND)}
        cur = {"_id": next(f"PV-{n:04d}" for n in range(1, 100_000) if f"PV-{n:04d}" not in taken), "key": key, "first_seen": _now(), "quotations": [], "wins": []}
    v = q["vendor"]
    words = sorted({w for w in re.findall(r"[a-z0-9]{3,}", " ".join([pr.get("title", ""), pr.get("brand", ""), " ".join(pr.get("specs") or [])]).lower())})
    quoted = [x for x in cur["quotations"] if x["pr_number"] != q["pr_number"]] + [
        {"pr_number": q["pr_number"], "title": pr.get("title", ""), "total_minor": q["total_minor"], "currency": q["currency"], "at": q["submitted_at"]}]
    rec = cur | {"name": v.get("legal_name") or v["name"], "email": v.get("email", ""), "phone": v.get("phone", ""), "website": v.get("website", ""),
                 "contact_person": v.get("contact_person", ""), "country": v.get("country_code", ""), "tax_id": v.get("tax_id", ""),
                 "registration_no": v.get("registration_no", ""), "vendor_id": q.get("vendor_id") or cur.get("vendor_id"), "source": q.get("source") or cur.get("source"),
                 "demo": bool(q.get("source") == "demo directory" or cur.get("demo")), "charge_account": (pr.get("charge_code") or "").split(".")[-1],
                 "keywords": sorted(set(cur.get("keywords") or []) | set(words))[:80], "quotations": quoted, "administration": admin(q), "last_seen": _now()}
    org._put(KIND, rec, f"vendor:{q['cand_id']}")
    return rec


def record_win(org, q: dict, po_id: str) -> None:
    key = _key(q["vendor"])
    cur = next((p for p in org.list(KIND) if p["key"] == key), None)
    if cur is not None:
        wins = [w for w in cur.get("wins") or [] if w["pr_number"] != q["pr_number"]] + [{"pr_number": q["pr_number"], "po_id": po_id, "at": _now()}]
        org._put(KIND, cur | {"wins": wins}, "award")


def match_preferred(org, pr: dict, words: set[str]) -> list[dict]:
    """The preferred vendors a request fits: they share its words or its charge account. Best match first."""
    acct = (pr.get("charge_code") or "").split(".")[-1]
    out = []
    for p in org.list(KIND):
        if not p.get("email"):
            continue
        shared = words & set(p.get("keywords") or [])
        score = len(shared) + (2 if acct and acct == p.get("charge_account") else 0)
        if score:
            out.append({"name": p["name"], "email": p["email"], "website": p.get("website", ""), "country": p.get("country", ""), "source": "preferred vendor",
                        "vendor_id": p.get("vendor_id"), "preferred_id": p["_id"], "demo": bool(p.get("demo")), "score": score + len(p.get("wins") or []),
                        "why": f"{p['_id']}: quoted {len(p.get('quotations') or [])} time(s), won {len(p.get('wins') or [])}" + (f"; fits on {', '.join(sorted(shared)[:4])}" if shared else "")})
    return sorted(out, key=lambda c: -c["score"])


# ---- ranking and conclusion ----

def ranking(pr: dict, qs: list[dict], a: dict) -> list[dict]:
    """Every quotation, best first: complete on the specifications before price, price among those, then administration."""
    rows = {r["cand_id"]: r for r in a["rows"]}
    cheapest = min((q["total_minor"] for q in qs), default=0)
    out = []
    for q in qs:
        r = rows[q["cand_id"]]
        met = sum(1 for s in q["specs"] if s["ok"]) - len([s for s in ((q.get("model_review") or {}).get("contradicts") or [])])
        ad = admin(q)
        out.append({"cand_id": q["cand_id"], "vendor": q["vendor"]["name"], "eligible": r["eligible"], "why_not": r["why_not"], "specs_met": max(met, 0), "specs_of": len(q["specs"]),
                    "total_minor": q["total_minor"], "currency": q["currency"], "above_cheapest_minor": q["total_minor"] - cheapest, "over_estimate": r["over_estimate"],
                    "administration": ad, "delivery_days": q.get("delivery_days"), "recommended": q["cand_id"] == a["recommended"]})
    out.sort(key=lambda x: (not x["eligible"], len(x["why_not"]), -(x["specs_met"] / x["specs_of"] if x["specs_of"] else 1), x["total_minor"], -x["administration"]["have"], x["cand_id"]))
    for n, x in enumerate(out, 1):
        x["rank"] = n
    return out


def conclusion(pr: dict, r: dict, qs: list[dict], a: dict) -> dict:
    """What stands for this PR now: {"state": awarded | needs_person | ready | waiting, "text"}."""
    if r.get("status") == "AWARDED":
        aw = r["award"]
        return {"state": "awarded", "text": f"Winner decided: {aw['vendor']} for {aw['currency']} {aw['total_minor'] / 100:,.2f}, by {aw['by']}" + (f" ({aw['note']})" if aw.get("note") else "")}
    invited = [c for c in r.get("candidates", []) if c.get("invited_at")]
    told = r.get("agent_decision") or {}
    if told.get("action") == "person":
        return {"state": "needs_person", "text": f"Needs a person's review: {told['reason']}"}
    if not qs:
        return {"state": "waiting", "text": f"Waiting for quotations: 0 of {len(invited)} invited vendor(s) answered; the deadline is {r.get('deadline') or '-'}"}
    if pr.get("status") != "APPROVED":
        return {"state": "needs_person", "text": f"Needs a person's approval: {pr['pr_number']} is {str(pr.get('status')).lower()}, and nothing is awarded on a request that is not approved"}
    if len(qs) < len(invited):
        return {"state": "waiting", "text": f"Waiting: {len(qs)} of {len(invited)} invited vendor(s) answered; the agent decides when all have, or after {r.get('deadline') or 'the deadline'}"}
    if a.get("recommended"):
        return {"state": "ready", "text": f"Ready for the award: {a['recommended']} {a['reason']}"}
    return {"state": "needs_person", "text": f"Needs a person's review: {a.get('suggestion') or a['reason']}"}


def events(org, r: dict, qs: list[dict]) -> list[dict]:
    """Everything that happened on one PR, oldest first: vendors invited, quotations received, emails queued and sent,
    the award. One PR number, every vendor."""
    out = []
    for c in r.get("candidates", []):
        if c.get("invited_at"):
            out.append({"at": c["invited_at"], "what": "RFQ sent", "who": c["name"], "detail": c.get("email", "")})
    for q in qs:
        out.append({"at": q["submitted_at"], "what": "quotation received" + (f" (revision {q['revision']})" if q.get("revision", 1) > 1 else ""), "who": q["vendor"]["name"],
                    "detail": f"{q['currency']} {q['total_minor'] / 100:,.2f}"})
    aw = r.get("award") or {}
    refs = {r["pr_number"], aw.get("po_id")} - {None}
    for m in org.list("outbox"):
        if m.get("ref") in refs and m.get("kind") in ("po_issued", "rfq_regret", "rfq_needs_person"):
            out.append({"at": m.get("sent_at") or m.get("created_at"), "who": ", ".join(m.get("to") or []), "detail": m.get("status", ""),
                        "what": {"po_issued": "winner announced", "rfq_regret": "regret letter", "rfq_needs_person": "a person was asked to decide"}[m["kind"]]})
    if aw:
        out.append({"at": aw["at"], "what": "awarded", "who": aw["vendor"], "detail": f"{aw.get('po_id')} by {aw['by']}"})
    return sorted(out, key=lambda e: e["at"] or "")


# ---- the PO threshold, the PO document, the winners ----

def settings(org) -> dict:
    rec = org.get(SETTINGS, SETTINGS_ID) or {}
    return DEFAULT_SETTINGS | {k: rec[k] for k in DEFAULT_SETTINGS if k in rec}


def save_settings(org, body: dict, by: str) -> dict:
    s = settings(org) | {k: body[k] for k in DEFAULT_SETTINGS if k in body}
    if not isinstance(s["po_threshold_minor"], int) or s["po_threshold_minor"] < 0:
        raise ValueError("the PO threshold is an amount of zero or more")
    s["po_threshold_currency"] = str(s["po_threshold_currency"]).upper()[:3]
    org._put(SETTINGS, {"_id": SETTINGS_ID} | s, by)
    return s


def po_required(org, total_minor: int, currency: str) -> tuple[bool, str]:
    """(a purchase order document is needed, why). The threshold is compared in its own currency; a purchase in another
    currency always gets a purchase order, since nothing here converts it."""
    s = settings(org)
    limit = f"{s['po_threshold_currency']} {s['po_threshold_minor'] / 100:,.2f}"
    if currency != s["po_threshold_currency"]:
        return True, f"purchase order required: the purchase is in {currency} and the threshold is set in {s['po_threshold_currency']}"
    if total_minor >= s["po_threshold_minor"]:
        return True, f"purchase order required: {currency} {total_minor / 100:,.2f} is at or above the {limit} threshold"
    return False, f"direct purchase: {currency} {total_minor / 100:,.2f} is below the {limit} threshold, so no purchase order document is needed"


def po_document(*, company: str, po: dict, pr: dict, q: dict, award: dict, required_note: str) -> str:
    """The draft purchase order as a printable page: buyer, supplier, the line, the terms, and who authorised it."""
    e = html.escape
    v = q["vendor"]
    unit = q.get("unit_price_minor", 0) / 100
    money = lambda m: f"{po['currency']} {m / 100:,.2f}"  # noqa: E731
    rows = [("Purchase order", po["po_id"]), ("Status", "DRAFT, for the supplier's confirmation"), ("Date", po.get("issued_date") or ""), ("Purchase request", pr["pr_number"]),
            ("Charge code", pr.get("charge_code") or ""), ("Deliver by", pr.get("needed_by") or "-"), ("Valid until", po.get("expiry_date") or "-")]
    head = "".join(f"<tr><td>{e(k)}</td><td><b>{e(str(x))}</b></td></tr>" for k, x in rows)
    supplier = "<br>".join(e(x) for x in [v.get("legal_name") or v["name"], v.get("street_address", ""), " ".join(filter(None, [v.get("city", ""), v.get("country_code", "")])),
                                          f"Tax id {v['tax_id']}" if v.get("tax_id") else "", f"{v.get('contact_person', '')} · {v.get('email', '')} · {v.get('phone', '')}"] if x.strip(" ·"))
    specs = "".join(f"<li>{e(s['spec'])}{' — ' + e(s['note']) if s.get('note') else ''}</li>" for s in q.get("specs") or [])
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Purchase order {e(po['po_id'])}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;color:#16213a;max-width:820px;margin:28px auto;padding:0 18px}}h1{{font-size:22px;margin:0}}h2{{font-size:14px;margin:18px 0 6px;text-transform:uppercase;letter-spacing:.06em;color:#556}}
table{{width:100%;border-collapse:collapse}}td,th{{padding:6px 8px;border-bottom:1px solid #e3e8f0;text-align:left;vertical-align:top}}th{{background:#f4f6fa}}.r{{text-align:right}}.two{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}
.m{{color:#667;font-size:12.5px}}.draft{{display:inline-block;padding:2px 10px;border:1px solid #b45309;color:#b45309;border-radius:99px;font-size:12px;font-weight:700}}@media print{{.noprint{{display:none}}}}</style>
<div style="display:flex;justify-content:space-between;align-items:flex-start"><div><h1>Purchase order</h1><div class=m>{e(company)}</div></div><span class=draft>DRAFT</span></div>
<div class=two><div><h2>Order</h2><table>{head}</table></div><div><h2>Supplier</h2><div>{supplier}</div><h2>Buyer</h2><div>{e(company)}<br><span class=m>Quote {e(po['po_id'])} on the invoice and on every delivery note.</span></div></div></div>
<h2>Goods or services</h2>
<table><tr><th>Description</th><th class=r>Quantity</th><th class=r>Unit price</th><th class=r>Amount</th></tr>
<tr><td><b>{e(pr['title'])}</b><div class=m>{e(' '.join(filter(None, [q.get('brand', ''), q.get('model', '')])))}</div><ul class=m style="margin:4px 0 0 16px;padding:0">{specs}</ul></td>
<td class=r>{q['quantity']:g} {e(pr.get('unit') or '')}</td><td class=r>{po['currency']} {unit:,.2f}</td><td class=r>{money(round(q.get('unit_price_minor', 0) * q['quantity']))}</td></tr>
<tr><td colspan=3 class=r>Delivery</td><td class=r>{money(q.get('delivery_minor', 0))}</td></tr><tr><td colspan=3 class=r>Tax</td><td class=r>{money(q.get('tax_minor', 0))}</td></tr>
<tr><th colspan=3 class=r>Total</th><th class=r>{money(po['total_minor'])}</th></tr></table>
<h2>Terms</h2>
<table><tr><td>Payment</td><td>{e(q.get('payment_terms') or 'on the invoice, after delivery')}</td></tr><tr><td>Delivery</td><td>within {e(str(q.get('delivery_days') or '-'))} day(s) of this order; by {e(pr.get('needed_by') or '-')}</td></tr>
<tr><td>Invoice</td><td>one invoice for the total above, quoting {e(po['po_id'])}; an invoice for another amount is looked at by a person before it is paid</td></tr>
<tr><td>Bank account</td><td>the account in the supplier's own name given on its supplier form; it is checked with the bank and confirmed by a call-back before the first payment</td></tr>
<tr><td>Basis</td><td>{e(required_note)}</td></tr></table>
<h2>Authorised</h2>
<table><tr><td>Purchase request approved by</td><td>{e(pr.get('decided_by') or '-')}</td></tr><tr><td>Awarded by</td><td>{e(award.get('by', ''))} on {e(str(award.get('at', ''))[:10])}</td></tr>
<tr><td>Reason</td><td>{e(award.get('note') or '')}</td></tr></table>
<p class="m noprint">This is a draft generated by VENTHORA from the awarded quotation. Print it or save it as a PDF from the browser.</p>"""


def winners(org, intake: list, quotes_of) -> list[dict]:
    """The vendor list: one row per awarded PR with where its purchase stands. `intake`: the payment requests;
    `quotes_of(pr_number)` -> the PR's quotations."""
    by_po: dict[str, list] = {}
    for req in intake:
        if getattr(req, "po_number", None):
            by_po.setdefault(req.po_number, []).append(req)
    forms = {f["po_id"]: f for f in org.list("vendor_details")}
    mail = [m for m in org.list("outbox") if m.get("kind") == "po_issued"]
    pv = {p["key"]: p for p in org.list(KIND)}
    out = []
    for r in org.list("rfq"):
        aw = r.get("award")
        if r.get("status") != "AWARDED" or not aw:
            continue
        pr = org.get("pr", r["pr_number"]) or {}
        q = next((x for x in quotes_of(r["pr_number"]) if x["cand_id"] == aw["cand_id"]), None) or {"vendor": {"name": aw["vendor"]}}
        need, why = (aw["po_required"], aw.get("po_note", "")) if "po_required" in aw else po_required(org, aw["total_minor"], aw["currency"])
        sent = next((m for m in mail if m.get("ref") == aw["po_id"]), None)
        form = forms.get(aw["po_id"]) or {}
        reqs = sorted(by_po.get(aw["po_id"], []), key=lambda x: x.received_at)
        paid = [x for x in reqs if x.payment]
        inv = reqs[-1] if reqs else None
        steps = [{"step": "Winner announced", "done": bool(sent and sent.get("status") == "sent"), "detail": (sent or {}).get("status", "not queued")},
                 {"step": "Purchase order" if need else "Direct purchase (no PO document)", "done": True, "detail": f"{aw['po_id']} · {'draft document ready' if need else 'reference for the invoice'}"},
                 {"step": "Supplier details and bank account", "done": bool(form.get("submitted_at")), "detail": form["submitted_at"][:16].replace("T", " ") if form.get("submitted_at") else "waiting for the supplier"},
                 {"step": "Invoice received", "done": inv is not None, "detail": f"{inv.invoice_no or '-'} · {inv.status}" if inv else "waiting for the supplier"},
                 {"step": "Paid", "done": bool(paid), "detail": f"execution {str(paid[-1].payment.get('execution_id', ''))[:8]}" if paid else ("held for a person" if inv and inv.status != "VERIFIED" else "not yet")}]
        out.append({"pr_number": r["pr_number"], "title": pr.get("title", ""), "vendor": aw["vendor"], "vendor_id": aw.get("vendor_id"),
                    "preferred_id": (pv.get(_key(q["vendor"])) or {}).get("_id"), "po_id": aw["po_id"], "po_required": need, "po_note": why,
                    "total_minor": aw["total_minor"], "currency": aw["currency"], "awarded_by": aw["by"], "awarded_at": aw["at"], "steps": steps,
                    "state": "paid" if paid else "invoice held" if inv and inv.status != "VERIFIED" else "invoice in payment" if inv else "waiting for the invoice",
                    "intake_ids": [x.intake_id for x in reqs], "contact": {k: (q["vendor"].get(k) or "") for k in ("contact_person", "email", "phone")}})
    return sorted(out, key=lambda x: x["awarded_at"], reverse=True)
