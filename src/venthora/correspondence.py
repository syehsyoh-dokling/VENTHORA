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
        return m.get("kind") in (None, "vendor_notice") and (ref in ids or (oid is not None and (ref == oid or ref.startswith(oid + "-"))))

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
