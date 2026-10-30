# ruff: noqa: E501
"""Document validation: ticked documents are mandatory, the invoice may not be dated after its submission, the bank
account on it must be the registered one, the stamp's size on the page is measured, edited PDFs are noticed, and
the model only adds findings it can quote."""
import asyncio
import datetime as dt
import json
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from venthora.intake import validation
from venthora.testing.pdfs import make_pdf

TODAY = dt.datetime.now(dt.UTC)
SENDER = "lumen.design.studio.billing@gmail.com"  # Lumen Design Studio's registered mailbox
STAMP = {"shape": "stamp", "w_mm": 38, "h_mm": 38, "ink": (30, 60, 170)}
SIGN = {"shape": "signature", "w_mm": 45, "h_mm": 15, "x": 90, "y": 120, "ink": (20, 20, 60)}


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _email(n: str, invoice_date: dt.date, attachments: list[tuple[str, bytes, str]], account: str = "483101090003") -> bytes:
    m = EmailMessage()
    m["From"] = f'"Lumen Design Studio" <{SENDER}>'
    m["To"] = "ap@acme.example"
    m["Subject"] = f"Invoice LD-{n}"
    m["Date"] = TODAY.strftime("%a, %d %b %Y %H:%M:%S +0000")
    m["Message-ID"] = f"<doc-{n}@test>"
    m["Authentication-Results"] = "mx.acme.example; spf=pass; dkim=pass"
    m.set_content(f"Hello, please find invoice LD-{n} for PO-2026-0101 attached.")
    for name, data, ctype in attachments:
        main, sub = ctype.split("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return m.as_bytes()


def _invoice_pdf(n, invoice_date, account="483101090003", images=(STAMP, SIGN), info=None):
    return make_pdf([f"Lumen Design Studio LLC - Invoice LD-{n}", f"Invoice date: {invoice_date.isoformat()}",
                     "Bill to: Acme Group", "PO-2026-0101 Phase 1", "Amount due: USD 1,800.00",
                     f"Due date: {(invoice_date + dt.timedelta(days=30)).isoformat()}", f"Bank of America account no: {account}"],
                    images=list(images), info=info)


def test_stamps_and_signatures_are_measured():
    docs = validation.read_documents(_email("1", TODAY.date(), [("inv.pdf", make_pdf(["Invoice"], images=[STAMP, SIGN, {"shape": "stamp", "w_mm": 70, "h_mm": 70, "ink": (200, 20, 30)}, {"shape": "logo", "w_mm": 40, "h_mm": 20, "ink": (240, 200, 0)}]), "application/pdf")]))
    marks = [(m["kind"], m["w_mm"]) for m in docs[0]["marks"]]
    assert marks == [("stamp", 38.0), ("signature", 45.0), ("stamp", 70.0), ("image", 40.0)]


def test_dates_are_read_day_first_or_month_first():
    assert validation.parse_date("03/04/2026") == dt.date(2026, 4, 3)
    assert validation.parse_date("03/04/2026", "mdy") == dt.date(2026, 3, 4)
    assert validation.parse_date("12 Oktober 2026") == dt.date(2026, 10, 12)
    assert validation.parse_date("Oct 12, 2026") == dt.date(2026, 10, 12)


def _rules(client, **kw):
    cur = client.get("/v1/settings/documents").json()["rules"]
    body = cur | {"required_documents": cur["required_documents"] | {"supplier_invoice": ["invoice", "delivery_note"]},
                  "checks": cur["checks"] | {"stamp_present": "required", "stamp_size": "required", "signature_present": "warn"}} | kw
    r = client.put("/v1/settings/documents", json=body)
    assert r.status_code == 200, r.text


def _post(client, raw):
    r = client.post("/v1/intake/emails", content=raw)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_clean_invoice_passes_every_check(client):
    _rules(client)
    day = TODAY.date() - dt.timedelta(days=3)
    raw = _email("901", day, [("LD-901.pdf", _invoice_pdf("901", day), "application/pdf"),
                              ("surat-jalan-901.pdf", make_pdf(["Surat Jalan / Delivery note", "Lumen Design Studio", "PO-2026-0101"]), "application/pdf")])
    rec = _post(client, raw)
    checks = {c["check"]: c for c in rec["checks"]}
    assert rec["status"] == "VERIFIED", [c for c in rec["checks"] if c["status"] == "fail"]
    assert checks["required_documents"]["status"] == "pass" and "Delivery note" in checks["required_documents"]["detail"]
    assert checks["stamp_size"]["status"] == "pass" and "38.0 x 38.0 mm" in checks["stamp_size"]["detail"]
    assert checks["bank_matches_registered"]["status"] == "pass" and checks["invoice_date_not_after_submission"]["status"] == "pass"
    assert checks["model_review"]["status"] == "skip"  # no model configured in the tests


def test_a_bad_invoice_is_held_with_every_reason(client):
    _rules(client)
    future = TODAY.date() + dt.timedelta(days=5)
    pdf = _invoice_pdf("902", future, account="999900001111", images=[{"shape": "stamp", "w_mm": 70, "h_mm": 70, "ink": (200, 20, 30)}],
                       info={"Producer": "iLovePDF", "CreationDate": TODAY.strftime("D:%Y%m%d%H%M%SZ")})
    rec = _post(client, _email("902", future, [("LD-902.pdf", pdf, "application/pdf"), ("notes.docx", b"PK..", "application/octet-stream")]))
    failed = {c["check"] for c in rec["checks"] if c["status"] == "fail"}
    assert {"required_documents", "invoice_date_not_after_submission", "bank_matches_registered", "stamp_size", "file_types_allowed",
            "pdf_not_edited", "signature_present"} <= failed
    assert rec["status"] == "NEEDS_REVIEW"
    assert any("missing: Delivery note" in r for r in rec["reasons"]) and any("1111, not the registered account •••0003" in r for r in rec["reasons"])
    assert "invoice_not_too_old" not in {c["check"] for c in rec["checks"]}  # a future date is the other check's failure
    assert "doc:signature_present" in rec["flags"]  # warn: flagged, but on its own it would not hold the invoice
    assert not any("signature" in r.lower() for r in rec["reasons"])


def test_checks_can_be_switched_off_and_validated(client):
    _rules(client, enabled=False)
    day = TODAY.date() + dt.timedelta(days=5)
    rec = _post(client, _email("903", day, [("LD-903.pdf", _invoice_pdf("903", day), "application/pdf")]))
    assert rec["checks"] == []
    assert client.put("/v1/settings/documents", json={"required_documents": {"supplier_invoice": ["passport"]}}).status_code == 422
    assert client.put("/v1/settings/documents", json={"stamp_min_mm": 60, "stamp_max_mm": 40}).status_code == 422
    assert client.put("/v1/settings/documents?role=auditor", json={}).status_code == 403


class _Model:
    configured = True

    async def complete(self, messages, max_tokens=300):
        return json.dumps({"documents": [{"attachment": "scan-001.pdf", "types": ["delivery_note"], "quote": "Goods handed to Acme warehouse"},
                                         {"attachment": "scan-001.pdf", "types": ["tax_invoice"], "quote": "an invented sentence"}],
                           "invoice_date": None,
                           "findings": [{"issue": "Amount in words differs from the figure", "quote": "one thousand dollars"},
                                        {"issue": "Made-up issue", "quote": "text that is not there"}]})


def test_the_model_adds_only_what_it_can_quote():
    docs = [{"name": "scan-001.pdf", "text": "Goods handed to Acme warehouse on 2 Oct. Total USD 1,800.00 (one thousand dollars)"}]
    out = asyncio.run(validation.model_assist(_Model(), "Invoice LD-904", "Invoice LD-904 attached", docs, TODAY.date()))
    assert out["types"] == {"delivery_note": {"where": "scan-001.pdf", "quote": "Goods handed to Acme warehouse", "source": "model"}}
    assert out["findings"] == [{"issue": "Amount in words differs from the figure", "quote": "one thousand dollars"}]
