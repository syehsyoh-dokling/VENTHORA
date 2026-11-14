# ruff: noqa: E501
"""The correspondence around one payment request, for the person who reviews it (Pending payment → Review).

Four things, all read from what VENTHORA already keeps (nothing is inferred from outside):
  vendor_messages   every message VENTHORA sent the vendor about this invoice (the outbox): when, to whom, its text,
                    and whether it was sent, is still queued, failed or bounced
  feedback          where the vendor stands: no message yet, waiting for an answer, answered and accepted, or answered
                    and still not accepted (with the reason)
  submissions       every other email the same vendor sent for the same invoice (a re-send, a correction, a reminder),
                    each with what VENTHORA decided and why: the log of what came back and was refused again
  fco_messages      every notice about this request sent to the FCO's mailbox: when, its status and its full text
"""
from __future__ import annotations

VENDOR_KINDS = (None, "vendor_notice", "vendor_hold", "vendor_reject")
# addresses that can never receive mail (reserved for tests and examples, RFC 2606): recorded, never sent
TEST_DOMAINS = (".example", ".test", ".invalid", ".localhost", "example.com", "example.org", "example.net")
INTERNAL = ("from the company's own system", "corrected by", "registered mailbox of", "reviewed by")
FLAG_FIELD = {"po_missing": ("po_number", "PO number", "the invoice quotes no purchase order"), "po_unknown": ("po_number", "PO number", "the PO quoted does not exist"),
              "po_not_open": ("po_number", "PO number", "the PO is closed or cancelled"), "po_expired": ("po_number", "PO number", "the PO has expired"),
              "po_supplier_mismatch": ("po_number", "PO number", "the PO is with another supplier"), "po_currency_mismatch": ("currency", "Currency", "the PO is in another currency"),
              "po_exceeded": ("amount_minor", "Amount", "the amount is above what the PO has left"), "po_term_mismatch": ("amount_minor", "Amount", "no PO term has this amount"),
              "payee_unknown": ("counterparty_id", "Pay to", "the payee is not registered"), "payee_currency_mismatch": ("currency", "Currency", "the payee is paid in another currency"),
              "duplicate_invoice": ("invoice_no", "Invoice / reference", "this invoice number is already in the queue")}


def is_test_address(addr: str) -> bool:
    dom = str(addr or "").rsplit("@", 1)[-1].lower()
    return not dom or any(dom == d.lstrip(".") or dom.endswith(d) for d in TEST_DOMAINS)


def vendor_reasons(req) -> list[str]:
    """Why the request is not paid, in words the vendor can act on (no internal notes)."""
    out = [r for r in (req.reasons or []) if not any(r.startswith(p) for p in INTERNAL) and "matched as its next payment" not in r]
    out += [f"{c.label}: {c.detail}" for c in (req.checks or []) if c.status == "fail" and c.mode == "required"]
    return list(dict.fromkeys(out))[:6] or ["it is held for a person to check"]


def compose_vendor(req, company: str, checked_at: str, rejected: bool = False) -> tuple[str, str]:
    """(subject, body) of the automatic answer to the vendor: which invoice, when it was received and checked, what is
    missing, and what to do."""
    inv = req.invoice_no or (req.subject or "your request")[:60]
    amt = f"{req.currency or ''} {(req.amount_minor or 0) / 100:,.2f}".strip()
    when = lambda t: f"{t[:10]} {t[11:16]} UTC" if t else "-"  # noqa: E731
    why = "\n".join(f"  - {r}" for r in vendor_reasons(req))
    verdict = "was rejected and will not be paid" if rejected else "cannot be paid as it stands"
    subject = f"Invoice {inv}: {'rejected' if rejected else 'received, not accepted for payment yet'}"
    body = (f"Dear {req.counterparty_name or req.display_name or 'Sir or Madam'},\n\n"
            f"{company or 'We'} received your invoice {inv} ({amt}) on {when(req.received_at)}. It was checked on {when(checked_at)} and {verdict}:\n\n{why}\n\n"
            + ("Please send a new invoice if the amount is still owed.\n" if rejected else
               f"Please send a corrected invoice to the same address, quoting invoice number {inv}. Nothing is paid until the points above are settled.\n")
            + f"\nThis is an automatic message from VENTHORA on behalf of {company or 'the company'}. A change of bank details is only ever confirmed by a call to your registered phone number.\n")
    return subject, body


def callback_sheet(req, payee: dict, phone: str) -> dict:
    """What the person who calls the vendor needs on one screen: the company, the person on the invoice, the registered
    number to call, and the fields to settle (from the flags and the failed checks)."""
    fields, seen = [], set()
    for f in req.flags or []:
        if f in FLAG_FIELD and f not in seen:
            seen.add(f)
            k, label, why = FLAG_FIELD[f]
            fields.append({"field": k, "label": label, "why": why})
    for c in req.checks or []:
        if c.status == "fail":
            fields.append({"field": None, "label": c.label, "why": c.detail})
    person = "" if req.trust == "system_of_record" else (req.display_name or "")
    return {"company": payee.get("name") or req.counterparty_name or req.claimed_name or "", "pic": person, "pic_email": "" if req.trust == "system_of_record" else req.sender,
            "phone": phone or "", "contacts": payee.get("contacts") or [], "to_correct": fields}


ACCEPTED = ("VERIFIED",)
REFUSED = {"REJECTED": "rejected", "DUPLICATE": "refused as a duplicate", "QUARANTINED": "quarantined", "NEEDS_REVIEW": "held for a person"}


def _msg(r: dict) -> dict:
    return {"id": r.get("_id"), "kind": r.get("kind"), "to": r.get("to") or [], "subject": r.get("subject"), "body": r.get("body"),
            "status": r.get("status"), "created_at": r.get("created_at"), "sent_at": r.get("sent_at"), "by": r.get("created_by"),
            "error": r.get("error"), "attempts": r.get("attempts", 0)}


def _same_invoice(a, b) -> bool:
    if a.intake_id == b.intake_id:
        return False
    if b.duplicate_of == a.intake_id or a.duplicate_of == b.intake_id:
        return True
    if a.invoice_fingerprint and a.invoice_fingerprint == b.invoice_fingerprint:
        return True
    same_party = (a.counterparty_id and a.counterparty_id == b.counterparty_id) or (a.sender and a.sender == b.sender)
    return bool(same_party and a.invoice_no and (b.invoice_no or "").upper() == a.invoice_no.upper())


def build(req, requests: list, outbox_items: list[dict]) -> dict:
    inv = (req.invoice_no or "").upper()
    oid = f"REQ-{inv}" if inv else None
    related = sorted((r for r in requests if _same_invoice(req, r)), key=lambda r: r.received_at)
    ids = {req.intake_id} | {r.intake_id for r in related}

    def about_vendor(m: dict) -> bool:
        ref = str(m.get("ref") or "")
        return m.get("kind") in VENDOR_KINDS and (ref in ids or (oid is not None and (ref == oid or ref.startswith(oid + "-"))))

    vendor = sorted((_msg(m) for m in outbox_items if about_vendor(m)), key=lambda m: m["created_at"] or "")
    fco = sorted((_msg(m) for m in outbox_items if m.get("kind") == "fco_notice" and str(m.get("ref") or "").split(":", 1)[-1] in ids),
                 key=lambda m: m["created_at"] or "")
    subs = [{"intake_id": r.intake_id, "received_at": r.received_at, "status": r.status, "outcome": "accepted" if r.status in ACCEPTED else REFUSED.get(r.status, r.status.lower()),
             "subject": r.subject, "sender": r.sender, "amount_minor": r.amount_minor, "currency": r.currency, "dunning_level": r.dunning_level,
             "reasons": list(r.reasons or [])[:4], "reviewer": r.reviewer, "review_note": r.review_note, "paid": bool(r.payment)} for r in related]
    delivered = [m for m in vendor if m["status"] == "sent"]
    last = (delivered or vendor)[-1] if vendor else None
    if last is None:
        fb = {"state": "none", "text": "No message has been sent to the vendor about this request."}
    elif not delivered:
        fb = {"state": "undelivered", "text": f"The message to the vendor is {last['status']}{': ' + last['error'] if last.get('error') else ''}; the vendor has not received it.",
              "since": last["created_at"]}
    else:
        after = [s for s in subs if (s["received_at"] or "") > (last["sent_at"] or last["created_at"] or "")]
        if not after:
            fb = {"state": "waiting", "text": "Sent; the vendor has not answered yet.", "since": last["sent_at"]}
        elif after[-1]["status"] in ACCEPTED:
            fb = {"state": "accepted", "text": "The vendor answered and the new submission passed every check.", "since": after[-1]["received_at"]}
        else:
            why = (after[-1]["reasons"] or ["no reason recorded"])[-1]
            fb = {"state": "refused_again", "text": f"The vendor answered, and it was {after[-1]['outcome']} again: {why}", "since": after[-1]["received_at"]}
    return {"intake_id": req.intake_id, "invoice_no": req.invoice_no, "vendor_messages": vendor, "feedback": fb, "submissions": subs,
            "refused": sum(1 for s in subs if s["status"] not in ACCEPTED), "fco_messages": fco}
