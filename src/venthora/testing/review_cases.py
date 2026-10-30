# ruff: noqa: E501
"""Invoices as they really go wrong: one email per case, with the outcome code must reach before anyone looks.

Two are clean and become bills ready to pay; one is a customer announcing a payment (incoming, not cleared); one is
forged and is quarantined; the rest stop for a person, each for a reason quoted from the email or its PDF. The same
cases run in the test suite (tests/test_review_cases.py) and live through the inbound mail webhook
(scripts/demo_review_cases.py), so the demo shows exactly what the tests prove.
"""
from __future__ import annotations

import base64
import datetime as dt

from .pdfs import make_pdf


def cases(stamp: str, today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    due = (today + dt.timedelta(days=7)).strftime("%d %b %Y")
    paid_on = (today + dt.timedelta(days=3)).strftime("%d %b %Y")
    spent = (today - dt.timedelta(days=2)).strftime("%d %b %Y")
    return [
        {"id": "ready-lumen", "title": "PDF invoice, partial on its PO", "tile": "ready",
         "sender": "lumen.design.studio.billing@gmail.com", "name": "Lumen Billing", "subject": f"Invoice LD-{stamp}1",
         "body": "Please find attached our invoice for the extra design round.",
         "pdf": [f"Invoice LD-{stamp}1", "Purchase order PO-2026-0101", "Extra design round  USD 650.00", "Subtotal USD 650.00",
                 "Tax USD 0.00", "Total due USD 650.00", f"Due {due}"],
         "expect": {"status": "VERIFIED", "flags": []}},
        {"id": "ready-thames", "title": "Partial call-off on an open PO", "tile": "ready",
         "sender": "thames.freight.accounts@gmail.com", "name": "Thames Freight", "subject": f"Invoice TF-{stamp}1",
         "body": f"Invoice TF-{stamp}1 for one container, against PO-2026-0103. Amount due: GBP 450.00, due {due}.",
         "expect": {"status": "VERIFIED", "flags": []}},
        {"id": "no-po", "title": "Invoice quotes no purchase order", "tile": "waiting",
         "sender": "billing@cloudloom.example", "name": "Cloudloom Billing", "subject": f"Invoice CL-{stamp}",
         "body": f"Invoice CL-{stamp}: 10 extra SaaS seats for November. Amount due: USD 450.00, due {due}.",
         "expect": {"status": "NEEDS_REVIEW", "flags": ["po_missing"]}},
        {"id": "po-exceeded", "title": "More than the PO has left", "tile": "waiting",
         "sender": "thames.freight.accounts@gmail.com", "name": "Thames Freight", "subject": f"Invoice TF-{stamp}2",
         "body": f"Invoice TF-{stamp}2, PO-2026-0103: December freight. Amount due: GBP 12,000.00, due {due}.",
         "expect": {"status": "NEEDS_REVIEW", "flags": ["po_exceeded"]}},
        {"id": "term-mismatch", "title": "An unscheduled amount is simply the PO's next termin", "tile": "ready",
         "sender": "debitoren@rheinwerk.example", "name": "Rheinwerk Debitoren", "subject": f"Rechnung RW-{stamp}1",
         "body": f"Invoice RW-{stamp}1, PO-2026-0102, interim delivery. Amount due: EUR 2,000.00, due {due}.",
         "expect": {"status": "VERIFIED", "flags": []}},
        {"id": "totals", "title": "Subtotal and tax do not make the total", "tile": "waiting",
         "sender": "lumen.design.studio.billing@gmail.com", "name": "Lumen Billing", "subject": f"Invoice LD-{stamp}2",
         "body": "Invoice attached.",
         "pdf": [f"Invoice LD-{stamp}2", "Purchase order PO-2026-0101", "Subtotal USD 1,000.00", "Tax USD 100.00",
                 "Total due USD 1,300.00", f"Due {due}"],
         "expect": {"status": "NEEDS_REVIEW", "flags": ["totals_do_not_add_up"]}},
        {"id": "bank-change", "title": "Asks to pay new bank details", "tile": "waiting",
         "sender": "mia.okafor.design@yahoo.com", "name": "Mia Okafor", "subject": f"Timesheet OK-{stamp}",
         "body": f"Timesheet OK-{stamp}, PO-2026-0104: 16 hours, total USD 800.00, due {due}. Please note my bank details have changed, "
                 "use the new account from now on.",
         "expect": {"status": "NEEDS_REVIEW", "flags": ["payee_bank_details_changed"], "kind": "bank_change"}},
        {"id": "urgent", "title": "Pressure: pay today, do not call", "tile": "waiting",
         "sender": "debitoren@rheinwerk.example", "name": "Rheinwerk Debitoren", "subject": f"URGENT Rechnung RW-{stamp}2",
         "body": f"URGENT: invoice RW-{stamp}2, PO-2026-0102, 30% deposit. Amount due: EUR 3,600.00. Please pay today without fail, do not call.",
         "expect": {"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}},
        {"id": "expense", "title": "Staff travel claim needs its approver", "tile": "waiting",
         "sender": "amira.shah@acme.example", "name": "Amira Shah", "subject": "Expense claim: Travel to Manchester",
         "body": f"Reimbursement for Travel: branch visit on {spent}, 2 days, train and hotel, total GBP 180.00 ({stamp}). Receipt attached.",
         "expect": {"status": "NEEDS_REVIEW", "flags": [], "kind": "expense_claim"}},
        {"id": "remittance", "title": "Customer announces a payment", "tile": "incoming",
         "sender": "ar@acme-retail.example", "name": "Acme Retail AR", "subject": f"Remittance advice AR-{stamp}",
         "body": f"Remittance advice AR-{stamp}: we will pay USD 6,500.00 on {paid_on} for invoice AR-{stamp}.",
         "expect": {"status": "VERIFIED", "flags": [], "kind": "remittance_notice"}},
        {"id": "forged", "title": "Registered mailbox, forged (SPF and DKIM fail)", "tile": "alert",
         "sender": "lumen.design.studio.billing@gmail.com", "name": "Lumen Billing", "subject": f"Invoice LD-{stamp}3",
         "body": f"Invoice LD-{stamp}3, PO-2026-0101. Amount due: USD 9,900.00, due {due}. Urgent.",
         "auth": "spf=fail; dkim=fail", "expect": {"status": "QUARANTINED", "flags": []}},
    ]


def postmark(case: dict, stamp: str) -> dict:
    """The case as Postmark's inbound JSON (what the mail service posts to /webhooks/email)."""
    att = [] if not case.get("pdf") else [{"Name": f"{case['subject'].split()[-1]}.pdf", "ContentType": "application/pdf",
                                           "Content": base64.b64encode(make_pdf(case["pdf"])).decode()}]
    return {"FromFull": {"Email": case["sender"], "Name": case["name"]}, "From": case["sender"], "To": "ap@inbound.example",
            "Subject": case["subject"], "Date": dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S +0000"),
            "MessageID": f"review-{stamp}-{case['id']}", "TextBody": case["body"],
            "Headers": [{"Name": "Authentication-Results", "Value": f"mx.inbound.example; {case.get('auth', 'spf=pass; dkim=pass')}"}],
            "Attachments": att}


def judge(case: dict, rec: dict) -> list[str]:
    """What differs from the expected outcome (empty = as expected)."""
    exp, bad = case["expect"], []
    if rec.get("status") != exp["status"]:
        bad.append(f"status {rec.get('status')} != {exp['status']}")
    if exp.get("kind") and rec.get("kind") != exp["kind"]:
        bad.append(f"kind {rec.get('kind')} != {exp['kind']}")
    missing = [f for f in exp["flags"] if f not in (rec.get("flags") or [])]
    if missing:
        bad.append("missing flags " + ", ".join(missing))
    return bad
