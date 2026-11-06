# ruff: noqa: E501
"""Procurement process: three quotations for a purchase request, a recommendation, a person's award, then the PO.

1. Vendor sourcing. For one purchase request (PR) the agent lists candidate vendors: registered vendors whose budget
   line fits, and vendors found online by the AI model (web search) when the model can search; otherwise the demo
   directory (data/procurement_directory.json, every address on example.test). Nothing is sent by finding.
2. Request for quotation (RFQ). A person picks the vendors and sends the invitation. Each vendor gets its own link to a
   form: company details, a checklist of the PR's specifications (complies + a remark per line), the brand offered,
   the price (unit price, delivery, tax), delivery time, validity, payment terms, an explanation and the quotation file.
   The link is a random token: a vendor sees only its own invitation. One vendor, one quotation; sending again before
   the award replaces the earlier one.
3. Quotation. Every quotation of a PR is one row. The recommendation keeps only quotations that meet every
   specification, offer the PR's brand when the PR names one (the brand is then locked), are in the PR's currency,
   cover the quantity and are still valid; the cheapest of those is recommended. When an AI model is set up it also
   reads each remark against its specification and marks a "complies" that the remark contradicts as not met.
4. Award. A person who may decide purchase requests (never the requester, within their approval limit) awards one
   quotation: the recommended one, or another with a written reason. The winner becomes a registered vendor if it is
   not one (NEW: nothing is paid until the call-back on its registered number), the PO is raised (PO-YYYY-NNNN, never
   above the approved estimate), the PR becomes ORDERED, and the PO and the regret letters are queued in the outbox.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import outbox
from .config import ROOT, data_path, env

DIRECTORY_FILE = ROOT / "data" / "procurement_directory.json"
MAX_FILE = 8 * 1024 * 1024
DECIDERS = ("finance_manager", "cfo", "super_admin", "country_finance_lead", "hq_admin", "country_admin")
# what a PR's account buys -> the budget lines of the registered vendors that may quote for it
ACCOUNT_LINES = {"6700": ("suppliers", "office"), "6500": ("suppliers",), "7100": ("suppliers",), "6300": ("saas",),
                 "6100": ("logistics",), "6200": ("contractors",), "6600": ("contractors", "suppliers")}


class SourcingError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def public_url() -> str:
    return (env("VENTHORA_PUBLIC_URL") or "https://venthora.muwahid.org").rstrip("/")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "vendor"


def rfq(org, pr_number: str) -> dict | None:
    return org.get("rfq", pr_number)


def quotes(org, pr_number: str | None = None) -> list[dict]:
    return [q for q in org.list("quote") if pr_number is None or q["pr_number"] == pr_number]


def _pr(org, pr_number: str) -> dict:
    pr = org.get("pr", pr_number)
    if pr is None:
        raise SourcingError(f"no purchase request {pr_number}")
    return pr


def _directory() -> list[dict]:
    try:
        return json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["vendors"]
    except (OSError, ValueError, KeyError):
        return []


def _words(pr: dict) -> set[str]:
    text = " ".join([pr.get("title", ""), pr.get("brand", ""), " ".join(pr.get("specs") or []), pr.get("justification", "")])
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


def _from_directory(pr: dict) -> list[dict]:
    want = _words(pr)
    acct = (pr.get("charge_code") or "").split(".")[-1]
    out = []
    for v in _directory():
        tags = set(v.get("tags") or [])
        score = len(want & tags) + (2 if acct in (v.get("accounts") or []) else 0)
        if score:
            out.append({"name": v["name"], "email": v["email"], "website": v.get("website", ""), "country": v.get("country", ""),
                        "source": "demo directory", "why": v.get("about", ""), "score": score, "demo": True})
    return sorted(out, key=lambda c: -c["score"])[:5]


def _registered(pr: dict, payees: dict) -> list[dict]:
    lines = ACCOUNT_LINES.get((pr.get("charge_code") or "").split(".")[-1], ())
    out = []
    for vid, p in payees.items():
        if p.get("budget_line") in lines and (p.get("currency") in (None, "", pr["currency"])) and p.get("contacts"):
            status = (p.get("lifecycle") or {}).get("status", "")
            if status in ("SUSPENDED", "BLOCKED"):
                continue
            out.append({"name": p["name"], "email": p["contacts"][0], "website": (p.get("domains") or [""])[0], "country": "",
                        "source": "registered vendor", "vendor_id": vid, "why": f"already a registered vendor ({p.get('budget_line')}, {status.lower() or 'on file'})"})
    return out[:3]


SEARCH_PROMPT = """You find suppliers for a company's purchase request. Search the web for real suppliers that sell
exactly this item and can deliver to {country}. Prefer distributors with a public sales or quotation email address.

Item: {title}
Quantity: {qty}
Brand required: {brand}
Specifications: {specs}

Answer with JSON only: {{"vendors": [{{"name": "...", "website": "https://...", "email": "sales@...", "country": "..",
"why": "one sentence: what they sell that fits"}}]}} with at most 5 vendors. Use only email addresses you saw on the
vendor's own website; leave email empty when you did not see one."""


async def _from_web(pr: dict, model, country: str) -> tuple[list[dict], str]:
    if model is None or not getattr(model, "configured", False) or not hasattr(model, "web_search"):
        return [], "the AI model cannot search the web (Claude through the Anthropic API can); used the demo directory"
    from .evidence.llm import LLMUnavailable, first_json_object

    prompt = SEARCH_PROMPT.format(country=country or "the requesting office", title=pr["title"], qty=f"{pr.get('quantity') or '-'} {pr.get('unit') or ''}".strip(),
                                  brand=pr.get("brand") or "none (any brand that meets the specifications)", specs="; ".join(pr.get("specs") or []) or "-")
    try:
        text = await model.web_search(prompt)
        found = first_json_object(text).get("vendors") or []
    except (LLMUnavailable, ValueError) as exc:
        return [], f"web search failed ({exc}); used the demo directory"
    def site(u) -> str:
        u = str(u or "").strip()[:200]
        return u if not u or u.startswith(("http://", "https://")) else f"https://{u}"

    out = [{"name": str(v.get("name", ""))[:120], "email": str(v.get("email") or "").strip().lower()[:120], "website": site(v.get("website")),
            "country": str(v.get("country") or "")[:40], "source": "web search", "why": str(v.get("why") or "")[:300]}
           for v in found if v.get("name")]
    return out[:5], f"{len(out)} vendor(s) found on the web by {getattr(model, 'model', 'the model')}"


async def find_vendors(org, pr_number: str, *, payees: dict, model=None, by: str) -> dict:
    """The candidates for a PR. Finding sends nothing; the invitation is a separate, human step."""
    pr = _pr(org, pr_number)
    if pr["status"] not in ("SUBMITTED", "APPROVED"):
        raise SourcingError(f"{pr_number} is {pr['status'].lower()}: vendors are sourced for a submitted or approved request")
    old = rfq(org, pr_number) or {}
    if old.get("status") == "AWARDED":
        raise SourcingError(f"{pr_number} is already awarded")
    unit = org.account_code(pr["charge_code"])["unit"]
    web, note = await _from_web(pr, model, unit.get("country") or unit.get("name") or "")
    # alpha-sandbox (demo mode on): the demo directory stays beside the web results, so the demo can show vendors
    # replying (VENTHORA_PROCUREMENT_DEMO_DIRECTORY=0 keeps it only as the fallback when the web gives nothing).
    # Demo mode off (the real version): never a demo vendor.
    from . import edition

    demo = edition.demo_on(org)
    demo_dir = demo and env("VENTHORA_PROCUREMENT_DEMO_DIRECTORY", "1") != "0"
    cands = _registered(pr, payees) + web + (_from_directory(pr) if demo_dir or (demo and not web) else [])
    if not demo and not web:
        note += "; demo mode is off, so no demo vendors: add vendors by hand"
    keep = {c["cand_id"]: c for c in old.get("candidates", []) if c.get("invited_at")}  # invited vendors stay
    seen = {(c["name"].lower(), c.get("email")) for c in keep.values()}
    for c in cands:
        if (c["name"].lower(), c.get("email")) in seen:
            continue
        seen.add((c["name"].lower(), c.get("email")))
        cid = _slug(c["name"])
        while cid in keep:
            cid += "-2"
        keep[cid] = c | {"cand_id": cid, "invited_at": None, "token": None}
    rec = {"_id": pr_number, "pr_number": pr_number, "status": old.get("status") or "SOURCED", "candidates": list(keep.values()),
           "deadline": old.get("deadline") or (date.today() + timedelta(days=5)).isoformat(), "search_note": note,
           "created_at": old.get("created_at") or _now(), "updated_at": _now(), "by": by, "award": None}
    org._put("rfq", rec, by)
    return rec


def add_vendor(org, pr_number: str, *, name: str, email: str, website: str = "", by: str) -> dict:
    """A vendor procurement already knows, added by hand."""
    r = rfq(org, pr_number) or {"_id": pr_number, "pr_number": pr_number, "status": "SOURCED", "candidates": [], "created_at": _now(),
                                "deadline": (date.today() + timedelta(days=5)).isoformat(), "award": None, "search_note": ""}
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""):
        raise SourcingError("a vendor needs an email address to receive the RFQ")
    cid = _slug(name)
    while any(c["cand_id"] == cid for c in r["candidates"]):
        cid += "-2"
    r["candidates"].append({"cand_id": cid, "name": name.strip()[:120], "email": email.strip().lower(), "website": website[:200], "country": "",
                            "source": "added by hand", "why": f"added by {by}", "invited_at": None, "token": None})
    org._put("rfq", r | {"updated_at": _now()}, by)
    return r


def _spec_lines(pr: dict) -> str:
    return "\n".join(f"  {i}. {s}" for i, s in enumerate(pr.get("specs") or [], 1)) or "  (see the item description)"


def rfq_letter(pr: dict, deadline: str, vendor: str = "[Vendor name]", link: str = "[the vendor's own quotation form link]") -> str:
    """The Request for Quotation letter: the draft kept on the PR, and the body every invited vendor receives."""
    brand = f"Brand: {pr['brand']} (only this brand is accepted)\n" if pr.get("brand") else ""
    return (f"Dear {vendor},\n\nWe invite you to quote for the following purchase request.\n\n"
            f"Reference: {pr['pr_number']}\nItem: {pr['title']}\nQuantity: {pr.get('quantity') or '-'} {pr.get('unit') or ''}\n{brand}"
            f"Specifications:\n{_spec_lines(pr)}\nCurrency: {pr['currency']}\nNeeded by: {pr.get('needed_by') or '-'}\n"
            f"Quotation deadline: {deadline}\n\nPlease submit your quotation through this form (it is yours only):\n{link}\n\n"
            "The form asks for your company details, a check of every specification, the price, delivery time, validity and your "
            "quotation document. Quotations sent any other way are not considered.\n\nProcurement team")


def draft_letter(org, pr_number: str, by: str) -> dict:
    """Keep the RFQ draft on the PR (Procurement → Vendor Sourcing shows it before anything is sent)."""
    pr = _pr(org, pr_number)
    deadline = ((rfq(org, pr_number) or {}).get("deadline") or (date.today() + timedelta(days=5)).isoformat())
    rec = {"_id": pr_number, "pr_number": pr_number, "subject": f"Request for quotation {pr_number}: {pr['title']}",
           "text": rfq_letter(pr, deadline), "deadline": deadline, "at": _now(), "by": by}
    org._put("rfq_draft", rec, by)
    return rec


def invite(org, pr_number: str, cand_ids: list[str], *, by: str, deadline: str | None = None) -> dict:
    """Queue one RFQ email per chosen vendor, each with its own form link."""
    pr = _pr(org, pr_number)
    r = rfq(org, pr_number)
    if r is None:
        raise SourcingError("find the vendors first")
    if r["status"] == "AWARDED":
        raise SourcingError(f"{pr_number} is already awarded")
    if deadline:
        r["deadline"] = deadline
    sent = []
    for c in r["candidates"]:
        if c["cand_id"] not in cand_ids:
            continue
        if not c.get("email"):
            raise SourcingError(f"{c['name']} has no email address; add one by hand")
        c["token"] = c.get("token") or secrets.token_urlsafe(18)
        link = f"{public_url()}/rfq/{c['token']}"
        body = rfq_letter(pr, r["deadline"], vendor=c["name"], link=link)
        outbox.queue(org, to=[c["email"]], subject=f"Request for quotation {pr_number}: {pr['title']}", body=body, ref=pr_number, by=by, kind="rfq_invite")
        c["invited_at"] = c.get("invited_at") or _now()
        sent.append(c["cand_id"])
    if not sent:
        raise SourcingError("choose at least one vendor")
    r["status"] = "INVITED"
    org._put("rfq", r | {"updated_at": _now()}, by)
    return r | {"sent": sent}


def by_token(org, token: str) -> tuple[dict, dict, dict] | None:
    for r in org.list("rfq"):
        for c in r["candidates"]:
            if c.get("token") and secrets.compare_digest(c["token"], token or ""):
                return r, c, org.get("pr", r["pr_number"])
    return None


def _minor(v, field: str) -> int:
    try:
        x = round(float(str(v).replace(",", "")) * 100)
    except (TypeError, ValueError) as exc:
        raise SourcingError(f"{field}: enter a number") from exc
    if x < 0 or x > 10**13:
        raise SourcingError(f"{field}: out of range")
    return x


def submit(org, token: str, form: dict) -> dict:
    """A vendor's quotation through its form link."""
    found = by_token(org, token)
    if found is None:
        raise SourcingError("this link is not valid")
    r, c, pr = found
    if r["status"] == "AWARDED":
        raise SourcingError("this request is already awarded; quotations are closed")
    if r.get("deadline") and date.today().isoformat() > r["deadline"]:
        raise SourcingError(f"the deadline {r['deadline']} has passed")
    v = form.get("vendor") or {}
    for k in ("name", "contact_person", "email", "phone"):
        if not str(v.get(k) or "").strip():
            raise SourcingError(f"vendor {k.replace('_', ' ')} is required")
    specs = pr.get("specs") or []
    got = form.get("specs") or []
    lines = []
    for i, s in enumerate(specs):
        g = got[i] if i < len(got) and isinstance(got[i], dict) else {}
        lines.append({"spec": s, "ok": bool(g.get("ok")), "note": str(g.get("note") or "")[:400]})
    qty = float(form.get("quantity") or 0)
    if qty <= 0:
        raise SourcingError("quantity: enter the quantity quoted")
    unit_price = _minor(form.get("unit_price"), "unit price")
    delivery, tax = _minor(form.get("delivery") or 0, "delivery"), _minor(form.get("tax") or 0, "tax")
    total = round(unit_price * qty) + delivery + tax
    if total <= 0:
        raise SourcingError("the quotation has no price")
    valid = str(form.get("valid_until") or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", valid):
        raise SourcingError("valid until: enter a date")
    f = form.get("file") or {}
    stored = None
    if f.get("data"):
        raw = base64.b64decode(f["data"].split(",")[-1])
        if len(raw) > MAX_FILE:
            raise SourcingError("the quotation file is larger than 8 MB")
        sha = hashlib.sha256(raw).hexdigest()
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(f.get("name") or "quotation"))[-80:]
        folder = data_path("VENTHORA_PROCUREMENT_DIR", "data/procurement") / r["pr_number"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{c['cand_id']}-{sha[:10]}-{name}").write_bytes(raw)
        stored = {"name": name, "size": len(raw), "sha256": sha, "path": f"{r['pr_number']}/{c['cand_id']}-{sha[:10]}-{name}"}
    old = org.get("quote", f"{r['pr_number']}:{c['cand_id']}")
    q = {"_id": f"{r['pr_number']}:{c['cand_id']}", "pr_number": r["pr_number"], "cand_id": c["cand_id"], "source": c.get("source"),
         "vendor_id": c.get("vendor_id"),
         "vendor": {k: str(v.get(k) or "").strip()[:200] for k in ("name", "legal_name", "registration_no", "tax_id", "contact_person", "email", "phone",
                                                                   "website", "street_address", "city", "country_code", "bank_name", "account_name",
                                                                   "account_number", "iban", "swift_code", "bank_country_code")},
         "brand": str(form.get("brand") or "").strip()[:80], "model": str(form.get("model") or "").strip()[:120], "specs": lines,
         "quantity": qty, "currency": pr["currency"], "unit_price_minor": unit_price, "delivery_minor": delivery, "tax_minor": tax, "total_minor": total,
         "delivery_days": int(form.get("delivery_days") or 0), "valid_until": valid, "payment_terms": str(form.get("payment_terms") or "")[:200],
         "explanation": str(form.get("explanation") or "")[:2000], "file": stored or (old or {}).get("file"),
         "submitted_at": _now(), "revision": ((old or {}).get("revision") or 0) + 1, "model_review": None}
    org._put("quote", q, f"vendor:{c['cand_id']}")
    c["quoted_at"] = q["submitted_at"]
    r["status"] = "QUOTING"
    org._put("rfq", r | {"updated_at": _now()}, f"vendor:{c['cand_id']}")
    return q


def assess(pr: dict, qs: list[dict], today: str | None = None) -> dict:
    """Per quotation: eligible or not and why; the recommended one (cheapest eligible)."""
    today = today or date.today().isoformat()
    brand = (pr.get("brand") or "").strip().lower()
    rows = []
    for q in qs:
        why = []
        unmet = [s["spec"] for s in q["specs"] if not s["ok"]]
        unmet += [s for s in ((q.get("model_review") or {}).get("contradicts") or []) if s not in unmet]
        if unmet:
            why.append(f"does not meet: {'; '.join(unmet)}")
        if brand and brand not in (q.get("brand") or "").lower():
            why.append(f"brand {q.get('brand') or 'not stated'}, the request asks for {pr['brand']}")
        if q["currency"] != pr["currency"]:
            why.append(f"quoted in {q['currency']}, not {pr['currency']}")
        if pr.get("quantity") and q["quantity"] < float(pr["quantity"]):
            why.append(f"quotes {q['quantity']:g} of {float(pr['quantity']):g}")
        if q["valid_until"] < today:
            why.append(f"expired on {q['valid_until']}")
        rows.append({"quote_id": q["_id"], "cand_id": q["cand_id"], "eligible": not why, "why_not": why, "total_minor": q["total_minor"],
                     "over_estimate": q["total_minor"] > pr["estimated_minor"]})
    ok = sorted((r for r in rows if r["eligible"]), key=lambda r: (r["total_minor"], r["cand_id"]))
    for n, r in enumerate(ok, 1):
        r["rank"] = n
    best = ok[0] if ok else None
    if best:
        others = [r for r in ok[1:]]
        gap = (others[0]["total_minor"] - best["total_minor"]) if others else None
        reason = (f"meets every specification{' and the ' + pr['brand'] + ' brand' if brand else ''}, and is the cheapest of "
                  f"{len(ok)} eligible quotation(s)" + ("" if gap is None else f", {gap / 100:,.2f} {pr['currency']} below the next" if gap
                                                         else f"; tied on price with {others[0]['cand_id']}, ranked first by vendor id: a person may choose either"))
    else:
        reason = "no quotation meets every condition yet"
    return {"rows": rows, "recommended": best["cand_id"] if best else None, "reason": reason, "locked_on": "brand and specifications" if brand else "specifications",
            "count": len(qs), "minimum": 3}


def shortlist(pr: dict, qs: list[dict], a: dict, size: int = 3) -> list[dict]:
    """Procurement → Shortlist: at most three vendors per PR, best first, each with its justification. Eligible
    quotations (every specification, brand, currency, quantity, validity) rank by total price; when fewer than three
    are eligible the best of the others fill the list, marked not eligible with the reason."""
    by_id = {q["cand_id"]: q for q in qs}
    ok = sorted((r for r in a["rows"] if r["eligible"]), key=lambda r: (r["total_minor"], r["cand_id"]))
    rest = sorted((r for r in a["rows"] if not r["eligible"]), key=lambda r: (len(r["why_not"]), r["total_minor"]))
    pick = (ok + rest)[:size]
    cheapest = ok[0]["total_minor"] if ok else None
    out = []
    for n, r in enumerate(pick, 1):
        q = by_id[r["cand_id"]]
        met = sum(1 for s in q["specs"] if s["ok"])
        why = [f"meets {met} of {len(q['specs'])} specification(s)" + (f", brand {q.get('brand')}" if q.get("brand") else "")]
        why.append(f"total {pr['currency']} {q['total_minor'] / 100:,.2f}" + ("" if cheapest is None or q["total_minor"] == cheapest
                                                                              else f", {(q['total_minor'] - cheapest) / 100:,.2f} above the cheapest eligible"))
        if q.get("delivery_days") is not None:
            why.append(f"delivers in {q['delivery_days']} day(s)")
        why.append(f"valid until {q['valid_until']}")
        if r["over_estimate"]:
            why.append("above the approved estimate: cannot be awarded without a new request")
        if not r["eligible"]:
            why.append("not eligible: " + "; ".join(r["why_not"]))
        out.append({"rank": n, "cand_id": r["cand_id"], "vendor": q["vendor"]["name"], "total_minor": q["total_minor"], "currency": q["currency"],
                    "eligible": r["eligible"], "recommended": r["cand_id"] == a["recommended"], "justification": why,
                    "model_note": (q.get("model_review") or {}).get("note") or ""})
    return out


REVIEW_PROMPT = """You check vendor quotations against a purchase request. For every quotation, look at each
specification the vendor marked as met together with the vendor's remark, and list the specifications whose remark
shows they are in fact NOT met (for example the request asks for 16 GB and the remark says 8 GB). Do not guess: when a
remark is empty or agrees, the specification is met.

Request: {title}
Specifications: {specs}

Quotations:
{quotes}

Answer with JSON only: {{"quotes": [{{"cand_id": "...", "contradicts": ["exact specification text"], "note": "one sentence"}}]}}"""


async def review(org, pr_number: str, model) -> dict:
    """The AI model reads every remark against its specification; contradicted 'complies' become not met."""
    pr = _pr(org, pr_number)
    qs = quotes(org, pr_number)
    if not qs:
        return {"basis": "rules", "note": "no quotation yet"}
    if model is None or not getattr(model, "configured", False):
        return {"basis": "rules", "note": "no AI model set up: the checklist answers are taken as given"}
    from .evidence.llm import LLMUnavailable, first_json_object

    text = "\n".join(f"- {q['cand_id']}: brand {q.get('brand') or '-'} {q.get('model') or ''}; " + "; ".join(
        f"[{'met' if s['ok'] else 'not met'}] {s['spec']} -- remark: {s['note'] or '(none)'}" for s in q["specs"]) for q in qs)
    try:
        out = first_json_object(await model.complete([{"role": "user", "content": REVIEW_PROMPT.format(
            title=pr["title"], specs="; ".join(pr.get("specs") or []), quotes=text)}], max_tokens=1200))
    except (LLMUnavailable, ValueError) as exc:
        return {"basis": "rules", "note": f"the model could not review ({exc})"}
    specs = set(pr.get("specs") or [])
    by = {str(x.get("cand_id")): x for x in out.get("quotes") or []}
    for q in qs:
        x = by.get(q["cand_id"]) or {}
        q["model_review"] = {"contradicts": [s for s in x.get("contradicts") or [] if s in specs], "note": str(x.get("note") or "")[:300], "at": _now()}
        org._put("quote", q, "agent")
    return {"basis": "model+rules", "note": f"remarks reviewed by {getattr(model, 'model', 'the model')}"}


def board(org, *, payees: dict | None = None) -> list[dict]:
    """Every PR in procurement with its invitations, quotations and the recommendation."""
    out = []
    for r in org.list("rfq"):
        pr = org.get("pr", r["pr_number"])
        if pr is None:
            continue
        qs = quotes(org, r["pr_number"])
        cands = {c["cand_id"]: c for c in r["candidates"]}
        rows = [q | {"candidate": {k: v for k, v in (cands.get(q["cand_id"]) or {}).items() if k != "token"}} for q in qs]
        a = assess(pr, qs)
        out.append({"pr": pr, "rfq": {k: v for k, v in r.items() if k != "candidates"} | {
            "candidates": [{k: v for k, v in c.items() if k != "token"} | {"form": bool(c.get("token"))} for c in r["candidates"]]},
            "quotes": sorted(rows, key=lambda q: q["total_minor"]), "assessment": a, "shortlist": shortlist(pr, qs, a),
            "draft": org.get("rfq_draft", r["pr_number"])})
    return sorted(out, key=lambda x: x["pr"]["pr_number"], reverse=True)


def _next_po(org) -> str:
    year = date.today().year
    nums = [int(m.group(1)) for p in org.list("po") if (m := re.fullmatch(rf"PO-{year}-(\d{{4,6}})", p["po_id"]))]
    return f"PO-{year}-{max(nums, default=0) + 1:04d}"  # the next after the last, never a gap in the sequence


def award(org, pr_number: str, cand_id: str, *, role: str, by: str, note: str, master, payees_store=None, registry_payees: dict) -> dict:
    """A person awards one quotation; the PO follows."""
    from . import ingestion
    from .organisation import PurchaseOrder

    pr = _pr(org, pr_number)
    r = rfq(org, pr_number)
    if r is None or r["status"] == "AWARDED":
        raise SourcingError(f"{pr_number} has nothing to award" if r is None else f"{pr_number} is already awarded to {r['award']['vendor']}")
    if pr["status"] != "APPROVED":
        raise SourcingError(f"{pr_number} is {pr['status'].lower()}: approve the purchase request before the award")
    if role not in DECIDERS:
        raise SourcingError(f"role {role} may not award a quotation")
    me = by.strip().lower()
    if me == pr["requested_by"].strip().lower():
        raise SourcingError("the requester cannot award their own purchase request")
    person = next((u for u in org.list("user") if me in (u["name"].lower(), u["email"].lower())), None)
    q = org.get("quote", f"{pr_number}:{cand_id}")
    if q is None:
        raise SourcingError(f"no quotation from {cand_id}")
    limit = (person or {}).get("approval_limit_minor")
    if limit is not None and q["total_minor"] > limit:
        raise SourcingError(f"{by} may approve up to {limit / 100:,.2f}; this quotation is {q['currency']} {q['total_minor'] / 100:,.2f}")
    a = assess(pr, quotes(org, pr_number))
    outside = cand_id != a["recommended"]
    if outside and len((note or "").strip()) < 10:
        raise SourcingError("awarding a quotation other than the recommendation needs the reason (at least 10 characters)")
    if q["total_minor"] > pr["estimated_minor"]:
        raise SourcingError(f"the quotation {q['currency']} {q['total_minor'] / 100:,.2f} is above the approved estimate "
                            f"{pr['currency']} {pr['estimated_minor'] / 100:,.2f}: raise a new request for the difference")
    if len(quotes(org, pr_number)) < 3 and len((note or "").strip()) < 10:
        raise SourcingError("fewer than 3 quotations: the award needs the reason (single or limited source)")
    vendor_id, v, followed = q.get("vendor_id"), q["vendor"], []
    if not vendor_id or vendor_id not in registry_payees:
        vendor_id = f"ven-{_slug(v.get('legal_name') or v['name'])}"
        rec = {"vendor_id": vendor_id, "name": v.get("legal_name") or v["name"], "currency": pr["currency"], "contacts": [v["email"]],
               "domains": [v["email"].split("@")[-1]], "registered_phone": v.get("phone", ""), "budget_line": "suppliers", "kind": "vendor",
               "tax_id": v.get("tax_id", ""), "address": {"street_address": v.get("street_address", ""), "city": v.get("city", ""),
                                                          "country_code": (v.get("country_code") or "")[:2].upper()}}
        if v.get("account_name") and (v.get("account_number") or v.get("iban")):
            rec["bank"] = {"account_name": v["account_name"], "account_currency": pr["currency"], "bank_name": v.get("bank_name", ""),
                           "bank_country_code": (v.get("bank_country_code") or v.get("country_code") or "US")[:2].upper(),
                           "account_number": v.get("account_number", ""), "iban": v.get("iban", ""), "swift_code": v.get("swift_code", "")}
            rec["payout_method"] = "SWIFT" if v.get("iban") or v.get("swift_code") else "LOCAL"
        report = ingestion.apply("vendor", [rec], [None], source=f"procurement:{pr_number}", master=master, org=org, payees=payees_store)
        if report["rejected"]:
            raise SourcingError(f"the vendor could not be registered: {report['rejected'][0]['error']}")
        followed = report.get("followed") or []
    po_id = _next_po(org)
    po = PurchaseOrder.model_validate({
        "po_id": po_id, "supplier_id": vendor_id, "currency": pr["currency"], "total_minor": q["total_minor"], "account_code": pr["charge_code"],
        "description": f"{pr['title']} ({q.get('brand') or ''} {q.get('model') or ''}, {q['quantity']:g} {pr.get('unit') or ''})".replace("( ", "(").replace("  ", " ")[:300],
        "issued_date": date.today().isoformat(), "start_date": date.today().isoformat(),
        "expiry_date": (date.today() + timedelta(days=max(30, (q.get("delivery_days") or 0) + 60))).isoformat(), "pr_number": pr_number})
    org.save("po", po, f"{by} ({role})")
    award_rec = {"cand_id": cand_id, "vendor": v["name"], "vendor_id": vendor_id, "po_id": po_id, "total_minor": q["total_minor"], "currency": q["currency"],
                 "by": f"{by} ({role})", "at": _now(), "note": note, "outside_recommendation": outside, "recommended": a["recommended"],
                 "quotes": len(quotes(org, pr_number)), "vendor_followed": followed}
    # the winner completes what the quotation did not ask (bank account, address, tax id) on its own form
    token = secrets.token_urlsafe(18)
    org._put("vendor_details", {"_id": token, "vendor_id": vendor_id, "pr_number": pr_number, "po_id": po_id, "vendor": v["name"],
                                "email": v["email"], "created_at": _now(), "submitted_at": None}, by)
    award_rec["details_form"] = f"/vendor-details/{token}"
    org._put("rfq", r | {"status": "AWARDED", "award": award_rec, "updated_at": _now()}, by)
    outbox.queue(org, to=[v["email"]], subject=f"Purchase order {po_id} for {pr_number}", ref=po_id, by=by, kind="po_issued",
                 body=(f"Dear {v.get('contact_person') or v['name']},\n\nYour quotation for {pr_number} ({pr['title']}) is accepted.\n\n"
                       f"Purchase order: {po_id}\nAmount: {q['currency']} {q['total_minor'] / 100:,.2f}\nQuantity: {q['quantity']:g} {pr.get('unit') or ''}\n"
                       f"Deliver by: {pr.get('needed_by') or '-'}\nCharge code: {pr['charge_code']}\n\n"
                       f"Please complete your company and bank details on this form (it is yours only), so we can pay you:\n{public_url()}/vendor-details/{token}\n\n"
                       f"Quote {po_id} on your invoice. Before the first payment your bank account is checked with the bank and we call you "
                       "back on your registered phone number to confirm it.\n\nProcurement team"))
    for other in quotes(org, pr_number):
        if other["cand_id"] != cand_id:
            outbox.queue(org, to=[other["vendor"]["email"]], subject=f"Request for quotation {pr_number}: result", ref=pr_number, by=by, kind="rfq_regret",
                         body=(f"Dear {other['vendor'].get('contact_person') or other['vendor']['name']},\n\nThank you for the time and care you put into "
                               f"your quotation for {pr_number} ({pr['title']}). After comparing every quotation against the specifications and price, "
                               "we have awarded this request to another supplier.\n\nWe value your offer and will keep you on our list for future "
                               "requests.\n\nWith thanks,\nProcurement team"))
    return award_rec | {"po": po.model_dump()}


def details_form(org, token: str) -> dict | None:
    return org.get("vendor_details", token) if token else None


DEMO_REPLIES = [  # filled forms of the demo directory vendors: one compliant and cheapest, one cheaper but off-spec, one dearer
    {"price": 0.92, "delivery": 15, "days": 3, "ok": True, "brand": True},
    {"price": 0.80, "delivery": 0, "days": 5, "ok": False, "brand": True},
    {"price": 1.00, "delivery": 25, "days": 2, "ok": True, "brand": True},
    {"price": 0.85, "delivery": 10, "days": 7, "ok": True, "brand": False},
]


def demo_replies(org, pr_number: str) -> list[dict]:
    """For the demo: the invited demo-directory vendors fill in their forms (never a real vendor)."""
    pr = _pr(org, pr_number)
    r = rfq(org, pr_number)
    if r is None:
        raise SourcingError("find and invite the vendors first")
    demo = [c for c in r["candidates"] if c.get("demo") and c.get("token") and not c.get("quoted_at")]
    if not demo:
        raise SourcingError("no invited demo-directory vendor is waiting to reply")
    qty = float(pr.get("quantity") or 1)
    unit_est = pr["estimated_minor"] / 100 / qty
    out = []
    for i, c in enumerate(demo):
        d = DEMO_REPLIES[i % len(DEMO_REPLIES)]
        specs = [{"ok": True, "note": "as specified"} for _ in pr.get("specs") or []]
        if not d["ok"] and specs:
            specs[-1] = {"ok": False, "note": "not available in this model; nearest alternative offered"}
        brand = pr.get("brand") if d["brand"] and pr.get("brand") else ("Generic" if pr.get("brand") else "OfficePro")
        dom = c["email"].split("@")[-1]
        out.append(submit(org, c["token"], {
            "vendor": {"name": c["name"], "legal_name": c["name"], "registration_no": f"REG-{1000 + i}", "tax_id": f"TAX-{7700 + i}",
                       "contact_person": "Sales desk", "email": c["email"], "phone": f"+1 555 010{i}", "website": f"https://{dom}",
                       "street_address": f"{10 + i} Market Street", "city": "Springfield", "country_code": "US",
                       "bank_name": "Demo Bank", "account_name": c["name"], "account_number": f"00012345{i}", "bank_country_code": "US"},
            "brand": brand, "model": pr.get("title", "")[:60], "specs": specs, "quantity": qty, "unit_price": round(unit_est * d["price"], 2),
            "delivery": d["delivery"], "tax": 0, "delivery_days": d["days"], "valid_until": (date.today() + timedelta(days=30)).isoformat(),
            "payment_terms": "30 days after delivery", "explanation": "Demo quotation filled on the vendor's behalf for the demo.",
            "file": {"name": f"quotation-{_slug(c['name'])}.txt", "data": base64.b64encode(f"Quotation {pr_number} from {c['name']}".encode()).decode()}}))
    return out


DEMO_PRS = [
    {"title": "HP 206A toner cartridges for the US office printers", "requested_by": "Amira Shah", "currency": "USD", "estimated_minor": 120_000,
     "charge_code": "US-200.OPS.6700", "quantity": 12, "unit": "pcs", "brand": "HP", "justification": "quarterly consumables; the printers take only HP 206A",
     "specs": ["Original HP 206A black toner (W2110A)", "Yield at least 1,350 pages each", "Delivered to the US-200 office within 7 days"], "_approve": True},
    {"title": "Ergonomic office chairs for new starters", "requested_by": "Amira Shah", "currency": "USD", "estimated_minor": 360_000,
     "charge_code": "US-200.OPS.6700", "quantity": 8, "unit": "pcs", "brand": "", "justification": "eight new starters in December",
     "specs": ["Adjustable lumbar support", "Seat height adjustable 42-52 cm", "Breathable mesh back", "Warranty at least 3 years"], "_approve": False},
]


def demo_requests(org, by: str) -> list[str]:
    """For the alpha-sandbox demo: two purchase requests from Intake (one approved with a brand, one waiting without)."""
    from .organisation import PurchaseRequest

    titles = {p["title"] for p in org.list("pr")}
    made = []
    for d in DEMO_PRS:
        if d["title"] in titles:
            continue
        rec = org.save("pr", PurchaseRequest.model_validate({k: v for k, v in d.items() if not k.startswith("_")} | {
            "needed_by": (date.today() + timedelta(days=21)).isoformat()}), by)
        if d["_approve"]:
            rec = org._put("pr", rec | {"status": "APPROVED", "decided_by": "Dana Finch (finance_manager)", "decided_at": _now(),
                                        "decision_note": "demo: approved within budget"}, by)
        made.append(rec["pr_number"])
    return made


def stored_file(pr_number: str, path: str) -> Path | None:
    base = data_path("VENTHORA_PROCUREMENT_DIR", "data/procurement").resolve()
    p = (base / path).resolve()
    return p if p.is_file() and base in p.parents and p.parent.name == pr_number else None
