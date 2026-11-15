# ruff: noqa: E501
"""An email asking for a cash advance, a travel advance, petty cash (a slush fund float) or an other direct cost
becomes a fund request, and, when the FCO sent it, is approved and paid at once as a virtual card.

1. Only a registered person (People & roles) raises one by email. From the FCO (Super admin) it is a request FOR the
   person it names ("for Dana Finch"), approved by the FCO's email; from anyone else it is their own request, SUBMITTED
   for a person to decide.
2. The AI model reads the email into the request (kind, person, amount, currency, purpose, activity, needed by, settle
   by, charge code); labelled lines ("Amount: USD 600") win over it; without a model the labelled lines alone are used.
3. An approved request is paid as a virtual Visa card on the card-issuing account: whole-life limit = the amount, valid
   until the settle-by date (api._issue_advance_card).
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

KINDS = {"cash_advance": ("cash advance", "travel advance", "travel and transport advance", "uang muka", "advance for"),
         "petty_cash": ("petty cash", "slush fund", "kas kecil", "float top-up", "petty-cash"),
         "odc": ("other direct cost", "direct cost", " odc ", "odc:")}
LABELS = {"for": "person", "employee": "person", "staff": "person", "untuk": "person", "amount": "amount", "jumlah": "amount", "nilai": "amount",
          "currency": "currency", "purpose": "purpose", "tujuan": "purpose", "keperluan": "purpose", "activity": "activity", "kegiatan": "activity",
          "needed by": "needed_by", "settle by": "settle_by", "receipts by": "settle_by", "charge code": "charge_code", "kode biaya": "charge_code",
          "type": "kind", "jenis": "kind"}
PROMPT = """Read this email asking the company for money in advance, and fill the request. Answer with one JSON object only:
{{"kind": "cash_advance (incl. travel and transport advances) | petty_cash (also slush fund / float) | odc (other direct cost)",
"person": "the full name or email of the person the money is for, if the email names one", "amount": number, "currency": "ISO code",
"purpose": "what the money is for, one line", "activity": "the trip, event or activity", "needed_by": "YYYY-MM-DD or empty",
"settle_by": "YYYY-MM-DD when receipts are due, or empty", "charge_code": "if the email gives one, else empty"}}
Use only what the email says; leave a field empty rather than guess.

Subject: {subject}
Email:
{body}"""


def looks_like(subject: str, body: str) -> bool:
    t = f" {subject}\n{body} ".lower()
    return any(k in t for ks in KINDS.values() for k in ks)


def _kind_of(text: str) -> str:
    t = f" {text} ".lower()
    for kind, words in KINDS.items():
        if any(w in t for w in words):
            return kind
    return "cash_advance"


def _labelled(body: str) -> dict:
    out = {}
    for line in body.splitlines():
        m = re.match(r"^\s*[-*]?\s*([A-Za-z][A-Za-z ]{1,24})\s*:\s*(.+?)\s*$", line)
        if m and m.group(1).strip().lower() in LABELS:
            out[LABELS[m.group(1).strip().lower()]] = m.group(2).strip()
    if "amount" in out:
        m = re.match(r"^\s*([A-Za-z]{3})\s*([\d.,]+)\s*$", out["amount"]) or re.match(r"^\s*([\d.,]+)\s*([A-Za-z]{3})\s*$", out["amount"])
        if m:
            a, b = m.groups()
            cur, num = (a, b) if a.isalpha() else (b, a)
            out.setdefault("currency", cur.upper())
            out["amount"] = num
    return out


def _num(v) -> float | None:
    from .amounts import parse_amount

    return parse_amount(v)  # one reader for every email that asks for money (amounts.py)


async def read(model, subject: str, body: str) -> tuple[dict, str]:
    fields, basis = {}, "labelled lines"
    if model is not None and getattr(model, "configured", False):
        from .evidence.llm import LLMUnavailable, first_json_object

        try:
            got = first_json_object(await model.complete([{"role": "user", "content": PROMPT.format(subject=subject, body=body[:6000])}], max_tokens=500))
            fields = {k: v for k, v in got.items() if v not in (None, "", [])}
            basis = f"{getattr(model, 'model', 'AI model')} + labelled lines"
        except (LLMUnavailable, ValueError) as exc:
            basis = f"labelled lines (the model could not read it: {str(exc)[:120]})"
    lab = _labelled(body)
    fields |= lab
    if "kind" in lab or not fields.get("kind") or fields.get("kind") not in KINDS:
        fields["kind"] = _kind_of(lab.get("kind") or f"{subject}\n{body}")
    return fields, basis


def person_of(org, name_or_email: str | None, sender: dict) -> dict | None:
    """The person the money is for: named in the email (by email or full name), else the sender."""
    if not name_or_email:
        return sender
    q = name_or_email.strip().lower()
    users = [u for u in org.list("user") if u.get("active", True)]
    return next((u for u in users if u["email"].lower() == q), None) or next((u for u in users if u["name"].lower() == q), None) \
        or next((u for u in users if q in u["name"].lower()), None)


def to_request(fields: dict, person: dict, sender: dict, forwarded: dict | None = None) -> dict:
    """The fund request record (validated by OrgStore). From the FCO for someone else: approved by the email. With the
    FCO's approval forwarded in it (advance_flow.forwarded_approval: {"from", "name", "quote"}): approved by that, as
    long as the FCO is not the person the money is for."""
    by_fco = sender.get("role") == "super_admin" and sender["user_id"] != person["user_id"]
    by_forward = bool(forwarded) and not by_fco and forwarded["from"].lower() != person["email"].lower()
    amount = _num(fields.get("amount"))
    if amount is None or amount <= 0:
        raise ValueError("the email names no amount")
    needed = fields.get("needed_by") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fields.get("needed_by") or "")) else None
    settle = fields.get("settle_by") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fields.get("settle_by") or "")) else None
    if fields["kind"] != "odc" and not settle:  # receipts are due 14 days after the money is needed, unless the email says
        settle = (date.fromisoformat(needed) if needed else date.today()) .__add__(timedelta(days=14)).isoformat()
    rec = {"kind": fields["kind"], "requested_by": person["user_id"], "amount_minor": int(round(amount * 100)),
           "currency": str(fields.get("currency") or "USD").upper()[:3], "charge_code": fields.get("charge_code") or person.get("charge_code") or "",
           "purpose": str(fields.get("purpose") or "advance requested by email")[:300], "activity": str(fields.get("activity") or "")[:160],
           "request_date": date.today().isoformat(), "needed_by": needed, "settle_by": settle if fields["kind"] != "odc" else None,
           "status": "APPROVED" if by_fco or by_forward else "SUBMITTED"}
    at = datetime.now(UTC).isoformat(timespec="seconds")
    if by_fco:
        rec |= {"decided_by": f"{sender['name']} (super_admin, by email)", "decision_note": "approved by the FCO's email", "decided_at": at}
    elif by_forward:
        rec |= {"decided_by": f"{forwarded.get('name') or forwarded['from']} (super_admin, email forwarded by {sender['name']})",
                "decision_note": f"approved in the FCO's email, forwarded: \"{forwarded['quote']}\""[:300], "decided_at": at}
    return rec
