# ruff: noqa: E501
"""Upload dataset and email sources: a CSV, Excel or PDF fills the same tables as a sync (mapped, cleaned, previewed,
then committed); a mailbox takes only what belongs there; the readiness page tells real data from placeholders."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from venthora import imports
from venthora.testing.pdfs import make_pdf
from venthora.testing.review_cases import postmark


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


VENDORS_CSV = (b"Supplier Name;Account Holder;IBAN;SWIFT/BIC;Bank Country;Currency;E-mail;Phone;Pay Method\n"
               b"Alpenblick Druck AG;Alpenblick Druck AG;CH93 0076 2011 6238 5295 7;UBSWCHZH80A;CH;EUR;billing@alpenblick.example;+41 44 555 0100;SWIFT\n"
               b"Baltic Freight OU;Baltic Freight OU;EE38 2200 2210 2014 5685;HABAEE2X;EE;EUR;invoices@balticfreight.example;+372 555 0101;SWIFT\n"
               b"Broken Bank Ltd;Broken Bank Ltd;NOT-AN-IBAN;XXXX;GB;GBP;a@broken.example;+44 1;LOCAL\n")


def test_csv_vendors_mapped_previewed_committed(client):
    up = client.post("/v1/sources/uploads?entity=vendor&filename=vendors.csv", content=VENDORS_CSV).json()
    assert up["kind"] == "csv" and up["rows"] == 3 and up["reader"] == "rules"
    m = up["mapping"]
    assert (m["name"], m["bank.iban"], m["contacts"], m["bank.swift_code"], m["registered_phone"]) == (
        "Supplier Name", "IBAN", "E-mail", "SWIFT/BIC", "Phone")
    assert m["bank.account_name"] == "Account Holder" and m["bank.bank_country_code"] == "Bank Country"
    mapping = m | {"bank.account_currency": "Currency"}
    pv = client.post(f"/v1/sources/uploads/{up['upload_id']}/preview", json={"mapping": mapping}).json()
    assert len(pv["created"]) == 2 and len(pv["rejected"]) == 1 and "IBAN" in pv["rejected"][0]["error"]
    assert pv["cleaned_sample"][0]["bank"]["iban"] == "CH9300762011623852957"
    assert client.get("/v1/sources/master/vendor").json() == []  # a preview writes nothing
    done = client.post(f"/v1/sources/uploads/{up['upload_id']}/commit", json={"mapping": mapping}).json()
    assert done["created"] == ["alpenblick-druck-ag", "baltic-freight-ou"]
    assert any("registered NEW" in f for f in done["followed"])
    payees = {p["beneficiary_id"]: p["status"] for p in client.get("/v1/payees").json()}
    assert payees["alpenblick-druck-ag"] == "NEW"


def test_excel_budget_lines(client):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Period", "Budget line", "Planned", "Alert"])
    ws.append(["2026-12", "Travel", "5,000.00", 9000])
    ws.append(["2026-12", "Marketing", 12000, 8000])
    buf = io.BytesIO()
    wb.save(buf)
    up = client.post("/v1/sources/uploads?entity=budget_line&filename=budget.xlsx", content=buf.getvalue()).json()
    assert up["kind"] == "xlsx" and up["mapping"] == {"period": "Period", "line": "Budget line", "amount_minor": "Planned", "alert_share_bp": "Alert"}
    done = client.post(f"/v1/sources/uploads/{up['upload_id']}/commit", json={"mapping": up["mapping"]}).json()
    assert sorted(done["created"]) == ["2026-12:marketing", "2026-12:travel"]
    from venthora.proofcore.budget import load_budget
    assert {ln.line: ln.amount_minor for ln in load_budget("2026-12").lines} == {"travel": 500_000, "marketing": 1_200_000}


def test_pdf_table_without_a_model(client):
    pdf = make_pdf(["Open items report", "Vendor  Invoice No  Amount  Currency  Due Date",
                    "ben-lumen-design  LD-801  1,250.00  USD  2026-11-20", "ben-uk-logistics  TF-802  480.00  GBP  2026-11-22"])
    up = client.post("/v1/sources/uploads?entity=bill&filename=open-items.pdf", content=pdf).json()
    assert up["kind"] == "pdf" and up["rows"] == 2
    assert up["mapping"]["vendor_id"] == "Vendor" and up["mapping"]["amount_minor"] == "Amount" and up["mapping"]["due_date"] == "Due Date"
    pv = client.post(f"/v1/sources/uploads/{up['upload_id']}/preview", json={"mapping": up["mapping"]}).json()
    assert len(pv["created"]) == 2 and pv["cleaned_sample"][0]["amount_minor"] == 125_000


class _Model:
    configured = True

    async def complete(self, messages, max_tokens=300):
        return json.dumps({"rows": [{"vendor_id": "ben-lumen-design", "invoice_no": "LD-901", "amount_minor": "1,250.00", "currency": "USD",
                                     "due_date": "2026-11-20"},
                                    {"vendor_id": "ben-lumen-design", "invoice_no": "LD-999", "amount_minor": "9,999.00"}]})


def test_pdf_rows_from_a_model_are_kept_only_when_in_the_text():
    import asyncio

    text = "Statement. Lumen ben-lumen-design invoice LD-901 amount 1,250.00 USD due 2026-11-20."
    rows = asyncio.run(imports.pdf_rows_with_model("bill", text, _Model()))
    assert rows[0] == {"vendor_id": "ben-lumen-design", "invoice_no": "LD-901", "amount_minor": "1,250.00", "currency": "USD", "due_date": "2026-11-20"}
    assert rows[1] == {"vendor_id": "ben-lumen-design"}  # LD-999 and 9,999.00 are not in the PDF: dropped


def test_mailboxes_route_what_arrives(client, monkeypatch):
    monkeypatch.setenv("VENTHORA_INBOUND_EMAIL_SECRET", "s3cret")
    for addr, purpose in (("ap@acme.example", "invoices"), ("claims@acme.example", "claims")):
        assert client.post("/v1/org/mailboxes", json={"address": addr, "purpose": purpose}).status_code == 200
    case = {"id": "x1", "sender": "lumen.design.studio.billing@gmail.com", "name": "Lumen", "subject": "Invoice LD-7101",
            "body": "Invoice LD-7101, PO-2026-0101. Amount due: USD 300.00, due 30 Nov 2026.", "pdf": None}
    p = postmark(case, "1") | {"OriginalRecipient": "claims@acme.example"}
    r = client.post("/webhooks/email", json=p, auth=("postmark", "s3cret")).json()
    rec = next(x for x in client.get("/v1/intake").json() if x["intake_id"] == r["intake_id"])
    assert rec["mailbox"] == "claims@acme.example" and "wrong_mailbox" in rec["flags"] and rec["status"] == "NEEDS_REVIEW"
    p2 = postmark(case | {"id": "x2", "subject": "Invoice LD-7102", "body": "Invoice LD-7102, PO-2026-0101. Amount due: USD 300.00, due 30 Nov 2026."}, "1") | {"OriginalRecipient": "ap@acme.example"}
    r2 = client.post("/webhooks/email", json=p2, auth=("postmark", "s3cret")).json()
    assert r2["status"] == "VERIFIED"


def test_readiness_tells_real_from_placeholder(client):
    items = {i["name"]: i for i in client.get("/v1/sources/readiness").json()["items"]}
    assert items["Vendors and bank details"]["status"] == "placeholder"
    assert items["Airwallex sandbox credentials"]["status"] == "missing"
    client.post("/v1/sources/uploads?entity=vendor&filename=v.csv", content=VENDORS_CSV)
    up = client.post("/v1/sources/uploads?entity=vendor&filename=v.csv", content=VENDORS_CSV).json()
    client.post(f"/v1/sources/uploads/{up['upload_id']}/commit", json={"mapping": up["mapping"] | {"bank.account_currency": "Currency"}})
    items = {i["name"]: i for i in client.get("/v1/sources/readiness").json()["items"]}
    assert items["Vendors and bank details"]["status"] == "ok" and "2 from the company's data" in items["Vendors and bank details"]["detail"]


def test_a_constant_fills_a_column_the_file_lacks(client):
    csv = b"Customer Name;E-mail\nNorthwind Traders;ap@northwind.example\n"
    up = client.post("/v1/sources/uploads?entity=customer&filename=c.csv", content=csv).json()
    pv = client.post(f"/v1/sources/uploads/{up['upload_id']}/preview", json={"mapping": up["mapping"]}).json()
    assert pv["rejected"] and "currency" in pv["rejected"][0]["error"]
    done = client.post(f"/v1/sources/uploads/{up['upload_id']}/commit", json={"mapping": up["mapping"] | {"currency": "=usd"}}).json()
    assert done["created"] == ["northwind-traders"]
    assert client.get("/v1/sources/master/customer").json()[0]["currency"] == "USD"


def test_the_checklist_judges_platform_setup_only(client):
    r = client.get("/v1/sources/readiness").json()
    items = {i["name"]: i for i in r["items"]}
    assert all(i["state"] in ("done", "required", "optional") and i["need"] in ("required", "optional") and i["fix"].startswith("#/") for i in r["items"])
    assert all(i["todo"] for i in r["items"] if i["state"] != "done") and all(not i["todo"] for i in r["items"] if i["state"] == "done")
    cred = items["Airwallex sandbox credentials"]  # no bank in a test: red, with what to do and where
    assert (cred["state"], cred["need"], cred["fix"]) == ("required", "required", "#/airwallex") and "client ID" in cred["todo"]
    # transactions are judged on their own pages, not here
    assert not {"Money in the Airwallex wallet", "A verified payee", "Purchase orders", "Open bills (AP)", "Receivables (AR)"} & set(items)
    assert not any(n.startswith("Budget for ") for n in items)
    assert items["AI model"]["need"] == "optional" and items["AI model"]["state"] in ("done", "optional")  # never red
    assert items["Customers"]["state"] in ("done", "optional")
    assert items["An approver other than who asks"]["state"] == "done"  # the seed has approvers
    assert items["A channel for payment requests"]["state"] == "done" and "payment orders from the dashboard" in items["A channel for payment requests"]["detail"]
    assert r["ready"] is False and r["states"]["required"] == sum(1 for i in r["items"] if i["state"] == "required")
