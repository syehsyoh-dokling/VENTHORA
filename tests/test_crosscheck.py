# ruff: noqa: E501
"""The end-to-end flow's joints: an invoice quoting a PO VENTHORA does not have pulls it from the connected source;
a photo or scan of an invoice is read by OCR; an internal payment order goes through the same checks and waits for
a different person; every execution result reaches the dashboards."""
import base64
import datetime as dt
import io
from email.message import EmailMessage
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi.testclient import TestClient

from venthora import connectors

TODAY = dt.datetime.now(dt.UTC)


@pytest.fixture
def client(monkeypatch):
    from venthora.api import app

    real = connectors.fetch

    async def through_app(raw, entity, client=None):  # the stand-in ERP is served by the app itself
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            return await real(raw, entity, client=c)
    monkeypatch.setattr(connectors, "fetch", through_app)
    with TestClient(app) as c:
        yield c


def _mail(sender, subject, body, mid, attach=()):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, "ap@acme.example", subject, f"<{mid}@test>"
    m["Date"] = TODAY.strftime("%a, %d %b %Y %H:%M:%S +0000")
    m["Authentication-Results"] = "mx; spf=pass; dkim=pass"
    m.set_content(body)
    for name, data, ctype in attach:
        main, sub = ctype.split("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return m.as_bytes()


def test_an_unknown_po_is_pulled_from_the_connected_source(client):
    client.get("/v1/sources/connectors")
    assert client.post("/v1/sources/connectors/legacy-erp-demo/sync?entity=vendor").status_code == 200  # vendors only, no POs yet
    assert client.get("/v1/org/po/PO-2026-0201").status_code in (404, 405) or "PO-2026-0201" not in str(client.get("/v1/org/pos").json())
    raw = _mail("rechnungen@nordlicht-logistik.example", "Rechnung NL-5009",
                "Invoice NL-5009, PO-2026-0201, racking deposit. Amount due: EUR 6,000.00, due 30 Nov 2026.", "nl5009")
    rec = client.post("/v1/intake/emails", content=raw).json()
    assert rec["po_number"] == "PO-2026-0201" and "po_unknown" not in rec["flags"]
    assert rec["reasons"][0] == "PO PO-2026-0201 was not in VENTHORA; pulled from Legacy ERP (demo) and matched again"
    assert rec["account_code"] == "DE-400.PRJ900.6100"
    raw = _mail("rechnungen@nordlicht-logistik.example", "Rechnung NL-5010", "Invoice NL-5010, PO-2099-0042. Amount due: EUR 100.00, due 30 Nov 2026.", "nl5010")
    rec = client.post("/v1/intake/emails", content=raw).json()
    assert "po_unknown" in rec["flags"] and rec["status"] == "NEEDS_REVIEW"
    assert any("none of the connected sources has it (asked: Legacy ERP (demo))" in r for r in rec["reasons"])


def _invoice_png(lines):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 70 * len(lines) + 40), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        pytest.skip("no TrueType font to draw a readable test invoice")
    for i, line in enumerate(lines):
        d.text((30, 20 + i * 70), line, fill="black", font=f)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_a_photo_of_an_invoice_is_read_by_ocr(client):
    pytest.importorskip("rapidocr_onnxruntime")
    png = _invoice_png(["Lumen Design Studio LLC", "Invoice LD-8801", "PO-2026-0101", "Amount due: USD 300.00"])
    raw = _mail("lumen.design.studio.billing@gmail.com", "Invoice attached", "Please see the attached scan.", "ld8801",
                [("scan-LD-8801.png", png, "image/png")])
    rec = client.post("/v1/intake/emails", content=raw).json()
    att = rec["attachments"][0]
    assert att["read"] and att["problem"] == "image; text read by OCR"
    assert rec["invoice_no"] == "LD-8801" and rec["amount_minor"] == 30_000 and rec["po_number"] == "PO-2026-0101"


def test_an_internal_payment_order_waits_for_someone_else(client):
    order = {"vendor_id": "ben-lumen-design", "invoice_no": "LD-9100", "amount_minor": 50_000, "currency": "USD",
             "due_date": (TODAY.date() + dt.timedelta(days=14)).isoformat(), "po_number": "PO-2026-0101", "description": "Design phase 1",
             "attachments": [{"name": "LD-9100.pdf", "content_type": "application/pdf",
                              "base64": base64.b64encode(__import__("venthora.testing.pdfs", fromlist=["make_pdf"]).make_pdf(
                                  ["Lumen Design Studio LLC", "Invoice LD-9100", "Bill to: Acme Group", "Amount due: USD 500.00"])).decode()}]}
    assert client.post("/v1/intake/payment-orders?by=Somebody%20Else", json=order).status_code == 403
    rec = client.post("/v1/intake/payment-orders?by=Dana%20Finch", json=order).json()
    assert rec["status"] == "NEEDS_REVIEW" and "internal_order" in rec["flags"] and rec["display_name"] == "Dana Finch"
    assert rec["po_number"] == "PO-2026-0101" and rec["account_code"] == "US-200.BRAND26.6600" and rec["checks"]
    assert rec["attachments"][0]["name"] == "LD-9100.pdf" and rec["attachments"][0]["read"]
    mine = client.post(f"/v1/intake/{rec['intake_id']}/review", json={"decision": "verify", "reviewer": "Dana Finch", "role": "finance_manager", "note": "x"})
    assert mine.status_code == 403
    ok = client.post(f"/v1/intake/{rec['intake_id']}/review", json={"decision": "verify", "reviewer": "Amira Shah", "role": "finance_manager", "note": "checked"})
    assert ok.status_code == 200 and ok.json()["status"] == "VERIFIED"
    again = client.post("/v1/intake/payment-orders?by=Dana%20Finch", json=order).json()
    assert again["status"] == "DUPLICATE"


def test_every_execution_result_is_announced():
    from venthora.execution.executor import Executor

    seen = []
    ex = NS(on_result=seen.append)
    result = NS(status="REFUSED")
    assert Executor._done(ex, result) is result and seen == [result]
    ex.on_result = lambda r: 1 / 0  # a broken listener never changes the outcome
    assert Executor._done(ex, result) is result


def test_invoice_and_po_numbers_in_their_usual_shapes():
    from venthora.intake.pipeline import _invoice
    from venthora.procurement import find_po

    shapes = {"Invoice LD-F025811 attached": "LD-F025811", "Invoice INV/2026/X/017 for October": "INV/2026/X/017",
              "Faktur no. INV-2026-A17": "INV-2026-A17", "Invoice LD-4410, PO-2026-0101": "LD-4410", "ref Q4-ALPHA/77": None}
    assert {t: (q.value if (q := _invoice(t, "")) else None) for t in shapes} == shapes
    assert find_po("Invoice POS-025437, PO-2026-0203.", "Invoice POS-025437")[0] == "PO-2026-0203"  # POS-... is no PO (seen live)
    assert find_po("P.O. No: 4500012345", "")[0] == "4500012345" and find_po("our PO number 2026/118", "")[0] == "2026/118"


def test_a_paid_request_shows_its_payment(client):
    from venthora.api import app

    raw = _mail("lumen.design.studio.billing@gmail.com", "Invoice LD-7711", "Invoice LD-7711, PO-2026-0101. Amount due: USD 120.00, due 30 Nov 2026.", "ld7711")
    rec = client.post("/v1/intake/emails", content=raw).json()
    assert client.get("/v1/intake").json()[0]["payment"] is None
    with app.state.executor.store._connect() as c:
        c.execute("INSERT INTO spend_ledger (transfer_id, book_id, obligation_id, invoice_fingerprint, currency, amount_minor, base_cost_minor, paid_at,"
                  " reporting_period, recorded_at, po_number) VALUES ('t-7711','b','o',?,'USD',12000,12000,'2026-10-30T03:00:00+00:00','2026-10','x','PO-2026-0101')",
                  (rec["invoice_fingerprint"],))
    got = next(r for r in client.get("/v1/intake").json() if r["intake_id"] == rec["intake_id"])
    assert got["payment"] == {"amount_minor": 12000, "currency": "USD", "transfers": ["t-7711"], "paid_at": "2026-10-30T03:00:00+00:00"}
