# ruff: noqa: E501
"""Invoices and claims as they really arrive: PDF attachments, staff expense claims checked against the organisation's
settings, the receiving server's SPF/DKIM verdicts, the inbound mail webhook, and a model whose reading must be quoted."""
import asyncio
import base64
import datetime as dt
import json
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from venthora.intake import documents, reader
from venthora.intake.models import ReviewRequest
from venthora.intake.pipeline import Intake, IntakeStore
from venthora.organisation import OrgStore


def make_pdf(lines: list[str]) -> bytes:
    """A minimal text-based PDF (one page, Helvetica), like an invoice exported from an accounting system."""
    esc = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines]
    stream = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(f"({t}) Tj T*" for t in esc) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def email(sender: str, subject: str, body: str, *, date="Fri, 24 Oct 2026 09:00:00 +0000", pdf: bytes | None = None,
          pdf_name="invoice.pdf", auth: str | None = None, msg_id: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"] = sender, "ap@acme.example", subject, date
    msg["Message-ID"] = msg_id or f"<{abs(hash((sender, subject, body)))}@test>"
    if auth:
        msg["Authentication-Results"] = auth
    msg.set_content(body)
    if pdf is not None:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=pdf_name)
    return msg.as_bytes()


LUMEN = "lumen.design.studio.billing@gmail.com"  # Lumen Design Studio's registered mailbox (data/payees.json)


@pytest.fixture
def org(tmp_path):
    return OrgStore(str(tmp_path / "org.db"))


@pytest.fixture
def intake(tmp_path, org):
    return Intake(IntakeStore(str(tmp_path / "intake.db")), org=org)


# -- PDF attachments and the invoice's own arithmetic ------------------------------------------------------------

def test_pdf_invoice_is_read_and_its_total_is_the_amount(intake):
    pdf = make_pdf(["Lumen Design Studio", "Invoice LD-91", "Brand refresh, phase 2   USD 1,000.00",
                    "Subtotal USD 1,000.00", "Tax (10%) USD 100.00", "Total due USD 1,100.00", "Due 14 Nov 2026"])
    r = intake.ingest(email(LUMEN, "Invoice LD-91", "Please find attached our invoice.", pdf=pdf))
    (att,) = r.attachments
    assert att.read and att.pages == 1 and att.problem is None and att.content_type == "application/pdf"
    assert (r.kind, r.trust, r.status, r.amount_minor, r.currency, r.invoice_no, r.due_date) == \
        ("supplier_invoice", "registered", "VERIFIED", 110_000, "USD", "LD-91", "2026-11-14")
    amount = next(q for q in r.quotes if q.field == "amount")
    assert amount.quote == "Total due USD 1,100.00" and r.text[amount.start:amount.end] == amount.quote


def test_totals_that_do_not_add_up_wait_for_a_person(intake):
    pdf = make_pdf(["Invoice LD-92", "Subtotal USD 1,000.00", "Tax USD 100.00", "Total due USD 1,300.00"])
    r = intake.ingest(email(LUMEN, "Invoice LD-92", "Invoice attached, due 20 Nov 2026.", pdf=pdf))
    assert r.status == "NEEDS_REVIEW" and "totals_do_not_add_up" in r.flags
    assert any("do not add up" in x for x in r.reasons)


def test_a_scan_without_text_is_listed_not_guessed(intake):
    scan = make_pdf([])  # a page with no text layer
    r = intake.ingest(email(LUMEN, "Invoice LD-93", "Invoice LD-93 attached: amount due USD 500.00, due 21 Nov 2026.", pdf=scan))
    (att,) = r.attachments
    assert not att.read and "no text layer" in att.problem
    assert r.amount_minor == 50_000  # read from the body, which says it in words
    assert documents.totals("no labels here")["adds_up"] is None


# -- sender authentication by the receiving server ---------------------------------------------------------------

def test_registered_mailbox_failing_spf_and_dkim_is_quarantined(intake):
    r = intake.ingest(email(LUMEN, "Invoice LD-94", "Invoice LD-94, amount due USD 900.00, due 25 Nov 2026.",
                            auth="mx.postmarkapp.com; spf=fail smtp.mailfrom=gmail.com; dkim=fail header.d=gmail.com"))
    assert (r.trust, r.status) == ("failed_authentication", "QUARANTINED")
    assert r.authentication == {"spf": "fail", "dkim": "fail"}
    ok = intake.ingest(email(LUMEN, "Invoice LD-95", "Invoice LD-95, amount due USD 900.00, due 25 Nov 2026.",
                             auth="mx.postmarkapp.com; spf=pass smtp.mailfrom=gmail.com; dkim=fail"))
    assert (ok.trust, ok.status) == ("registered", "VERIFIED")
    # seen live: a forged resend of a real invoice number must raise the alarm, not pass as a mere duplicate
    forged = intake.ingest(email(LUMEN, "Invoice LD-95", "Invoice LD-95, amount due USD 9,900.00, due 25 Nov 2026. Urgent.",
                                 auth="spf=fail; dkim=fail", msg_id="<forged-resend@test>"))
    assert forged.status == "QUARANTINED" and forged.duplicate_of == ok.intake_id


# -- staff expense claims against User management, Finance standards and Unit prices -----------------------------

def test_expense_claim_is_checked_and_routed_never_to_the_claimant(intake):
    r = intake.ingest(email("dana.finch@acme.example", "Expense claim: Travel to Chicago",
                            "Hi, reimbursement for Travel: client visit on 20 Oct 2026, 1 day, total USD 150.00. Receipt attached."))
    assert (r.kind, r.trust, r.status, r.amount_minor, r.currency) == ("expense_claim", "staff", "NEEDS_REVIEW", 15_000, "USD")
    assert (r.counterparty_name, r.unit_id, r.category, r.expense_date) == ("Dana Finch", "co-us", "Travel", "2026-10-20")
    assert "above_unit_price" in r.flags  # per diem United States is USD 75 a day for the US country office
    assert r.approvers == ["u-omar"]  # Dana is the finance manager of her own unit, so the CFO approves
    assert any("never approves their own claim" in x for x in r.reasons)


def test_expense_claim_rules_deadline_category_currency(intake):
    late = intake.ingest(email("jonas.weber@acme.example", "Expenses", "Out of pocket for the warehouse visit on 1 Aug 2026: EUR 60.00."))
    assert {"claim_past_deadline", "category_unknown"} <= set(late.flags)
    eur = intake.ingest(email("jonas.weber@acme.example", "Travel expenses",
                              "Travel to the Hamburg site on 22 Oct 2026, 3 days, EUR 84.00 per diem, receipt attached."))
    assert eur.category == "Travel" and "above_unit_price" not in eur.flags  # 3 x EUR 28 for the Germany office chain
    assert eur.approvers == ["u-omar"] and any("not compared" in x for x in eur.reasons)


def test_expense_claim_review_rules(intake):
    r = intake.ingest(email("dana.finch@acme.example", "Expense claim Travel",
                            "Travel on 21 Oct 2026, USD 60.00 per diem, receipt attached."))
    with pytest.raises(PermissionError, match="own claim"):
        intake.review(r.intake_id, ReviewRequest(decision="verify", reviewer="Dana Finch", role="cfo"))
    with pytest.raises(PermissionError, match="may not approve"):
        intake.review(r.intake_id, ReviewRequest(decision="verify", reviewer="Lee Park", role="treasury_analyst"))
    ok = intake.review(r.intake_id, ReviewRequest(decision="verify", reviewer="Omar Reyes", role="cfo", note="receipt checked"))
    assert ok.status == "VERIFIED"
    again = intake.ingest(email("dana.finch@acme.example", "Expense claim Travel",
                                "Travel on 21 Oct 2026, USD 60.00 per diem, receipt attached.", msg_id="<resent@test>"))
    assert again.status == "DUPLICATE"


def test_a_company_domain_is_not_staff(intake):
    r = intake.ingest(email("someone@acme.example", "Expense claim", "Travel on 20 Oct 2026, USD 900.00, please reimburse."))
    assert r.trust == "unknown_sender" and r.kind != "expense_claim" and r.status in ("QUARANTINED", "NEEDS_REVIEW")


# -- the model reads, code decides ---------------------------------------------------------------------------------

class _Model:
    configured = True

    def __init__(self, reply: dict):
        self.reply = reply

    async def complete(self, messages, max_tokens=300):
        return "Here you go:\n" + json.dumps(self.reply)


TEXT = "Hello,\nour reference is Q4-ALPHA/77 for the October work.\nPlease remit the sum of 2.450,00 EUR by 30 November 2026.\n"


def test_reader_keeps_only_what_the_text_proves():
    got = asyncio.run(reader.read(TEXT, "October", dt.date(2026, 10, 24), _Model({
        "amount": {"value": "2.450,00", "currency": "EUR", "quote": "the sum of 2.450,00  EUR"},
        "invoice_no": {"value": "Q4-ALPHA/77", "quote": "reference is Q4-ALPHA/77"},
        "due_date": {"value": "2026-11-30", "quote": "by 30 November 2026"}})))
    assert got["amount"].value == "EUR 245000" and TEXT[got["amount"].start:got["amount"].end] == got["amount"].quote
    assert got["invoice_no"].value == "Q4-ALPHA/77" and got["due_date"].value == "2026-11-30"
    invented = asyncio.run(reader.read(TEXT, "October", dt.date(2026, 10, 24), _Model({
        "amount": {"value": "24500", "currency": "EUR", "quote": "the sum of 2.450,00 EUR"},  # value not in the quote
        "invoice_no": {"value": "INV-1", "quote": "invoice INV-1"},  # quote not in the text
        "due_date": {"value": "2026-12-30", "quote": "by 30 November 2026"}})))  # month does not match... day does
    assert "amount" not in invented and "invoice_no" not in invented


def test_model_fills_gaps_and_disagreement_goes_to_a_person(intake):
    body = "Hello, our reference is Q4-ALPHA/77. Please remit the sum of 2,450.00 EUR by 30 Nov 2026."
    raw = email(LUMEN, "October work", body)
    from venthora.intake.pipeline import parse_eml
    text = parse_eml(raw)["text"]
    proposals = asyncio.run(reader.read(text, "October work", dt.date(2026, 10, 24), _Model({
        "amount": {"value": "2,450.00", "currency": "EUR", "quote": "2,450.00 EUR"},
        "invoice_no": {"value": "Q4-ALPHA/77", "quote": "reference is Q4-ALPHA/77"}})))
    r = intake.ingest(raw, proposals=proposals)
    assert r.reader == "model+rules" and r.invoice_no == "Q4-ALPHA/77" and r.amount_minor == 245_000
    other = intake.ingest(email(LUMEN, "Invoice LD-96", "Invoice LD-96: amount due USD 700.00, due 26 Nov 2026."),
                          proposals={"amount": reader.Quote(field="amount", value="USD 7000", quote="USD 700.00", start=0, end=0)})
    assert other.status == "NEEDS_REVIEW" and "reader_disagreement" in other.flags


# -- the inbound mail webhook ------------------------------------------------------------------------------------

def postmark(pdf: bytes) -> dict:
    return {"FromFull": {"Email": LUMEN, "Name": "Lumen Billing"}, "From": LUMEN, "To": "abc123@inbound.postmarkapp.com",
            "Subject": "Invoice LD-97", "Date": "Fri, 24 Oct 2026 10:00:00 +0000", "MessageID": "pm-0001",
            "TextBody": "Invoice attached.", "Headers": [{"Name": "Authentication-Results", "Value": "spf=pass; dkim=pass"}],
            "Attachments": [{"Name": "LD-97.pdf", "ContentType": "application/pdf",
                             "Content": base64.b64encode(pdf).decode()}]}


def test_inbound_email_webhook(monkeypatch):
    from venthora.api import app
    pdf = make_pdf(["Invoice LD-97", "Subtotal USD 400.00", "VAT USD 40.00", "Amount due USD 440.00", "Due 28 Nov 2026"])
    with TestClient(app) as client:
        assert client.post("/webhooks/email", json=postmark(pdf)).status_code == 503
        monkeypatch.setenv("VENTHORA_INBOUND_EMAIL_SECRET", "inbound-test-secret")
        assert client.post("/webhooks/email", json=postmark(pdf), auth=("postmark", "wrong")).status_code == 401
        r = client.post("/webhooks/email", json=postmark(pdf), auth=("postmark", "inbound-test-secret"))
        assert r.status_code == 200 and r.json()["status"] == "VERIFIED"
        again = client.post("/webhooks/email", json=postmark(pdf), auth=("postmark", "inbound-test-secret"))
        assert again.json()["intake_id"] == r.json()["intake_id"]  # the same message is never queued twice
        rec = next(x for x in client.get("/v1/intake").json() if x["intake_id"] == r.json()["intake_id"])
        assert rec["amount_minor"] == 44_000 and rec["authentication"] == {"spf": "pass", "dkim": "pass"}
        assert rec["attachments"][0]["name"] == "LD-97.pdf" and rec["attachments"][0]["read"]
        raw = email(LUMEN, "Invoice LD-98", "Invoice LD-98, amount due USD 300.00, due 29 Nov 2026.")
        r2 = client.post("/webhooks/email", content=raw, headers={"content-type": "message/rfc822"}, auth=("x", "inbound-test-secret"))
        assert r2.status_code == 200 and r2.json()["kind"] == "supplier_invoice"
