# ruff: noqa: E501
"""Dataset upload takes any data file: VENTHORA decides what it holds (entity=auto), reads Excel, CSV, JSON, Word,
HTML and e-mail, and every kind of data VENTHORA keeps has its entry, with the page its records show on."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from venthora import imports
from test_uploads import VENDORS_CSV


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _docx(rows: list[list[str]]) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    cells = lambda r: "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r)  # noqa: E731
    body = f"<w:p><w:r><w:t>Vendor list</w:t></w:r></w:p><w:tbl>{''.join(f'<w:tr>{cells(r)}</w:tr>' for r in rows)}</w:tbl>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


def test_every_kind_of_data_has_an_entry_and_a_page(client):
    kinds = {k["entity"]: k for k in client.get("/v1/sources/entities").json()}
    assert set(kinds) == set(imports.FIELDS)  # nothing VENTHORA imports is missing from the dropdown
    assert {"unit", "user", "project", "account", "price", "vendor", "customer", "po", "bill", "receivable", "budget_line", "allocation", "tenant_book"} <= set(kinds)
    assert all(k["page"].startswith("#/") and k["label"] and k["why"] for k in kinds.values())
    assert kinds["allocation"]["page"] == "#/allocation" and kinds["vendor"]["page"] == "#/payee-lifecycle"


def test_auto_detects_vendors_from_their_columns(client):
    up = client.post("/v1/sources/uploads?entity=auto&filename=suppliers.csv", content=VENDORS_CSV).json()
    assert up["entity"] == "vendor" and up["detected"]["by"] == "column names" and up["page"] == "#/payee-lifecycle"
    assert up["mapping"]["bank.iban"] == "IBAN"
    up2 = client.post("/v1/sources/uploads?filename=suppliers.csv", content=VENDORS_CSV).json()  # auto is the default
    assert up2["entity"] == "vendor"


def test_json_word_and_html_are_read_as_tables(client):
    recs = {"data": [{"Supplier Name": "Nordwind GmbH", "IBAN": "DE89 3704 0044 0532 0130 00", "SWIFT/BIC": "COBADEFFXXX", "Bank Country": "DE", "Currency": "EUR"}]}
    j = client.post("/v1/sources/uploads?entity=auto&filename=v.json", content=json.dumps(recs).encode()).json()
    assert (j["kind"], j["entity"], j["rows"]) == ("json", "vendor", 1)
    grid = [["Supplier Name", "IBAN", "SWIFT/BIC", "Bank Country", "Currency"], ["Nordwind GmbH", "DE89370400440532013000", "COBADEFFXXX", "DE", "EUR"]]
    d = client.post("/v1/sources/uploads?entity=auto&filename=v.docx", content=_docx(grid)).json()
    assert (d["kind"], d["entity"], d["rows"]) == ("docx", "vendor", 1) and d["mapping"]["bank.iban"] == "IBAN"
    html = "<html><body><p>PO list</p><table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in grid) + "</table></body></html>"
    h = client.post("/v1/sources/uploads?entity=vendor&filename=v.html", content=html.encode()).json()
    assert (h["kind"], h["rows"]) == ("html", 1) and h["mapping"]["name"] == "Supplier Name"


def test_a_photo_or_old_spreadsheet_says_what_to_upload_instead(client):
    r = client.post("/v1/sources/uploads?entity=auto&filename=scan.jpg", content=b"\xff\xd8\xff\xe0fake")
    assert r.status_code == 422 and "PDF" in r.json()["detail"]
    r = client.post("/v1/sources/uploads?entity=auto&filename=old.xls", content=b"\xd0\xcf\x11\xe0")
    assert r.status_code == 422 and ".xlsx" in r.json()["detail"]


def test_budget_allocations_upload_by_cost_centre_and_update_on_the_second_run(client):
    unit = next(u for u in client.get("/v1/org/units").json() if u["kind"] == "COUNTRY_OFFICE" and u.get("cost_centre"))
    csv = f"Budget Period,Cost Centre,Budget Line,Allocated Amount,Currency\n2031-01,{unit['cost_centre']},Operations,\"12,500.00\",{unit['currency']}\n".encode()
    up = client.post("/v1/sources/uploads?entity=auto&filename=alloc.csv", content=csv).json()
    assert up["entity"] == "allocation" and up["page"] == "#/allocation"
    done = client.post(f"/v1/sources/uploads/{up['upload_id']}/commit", json={"mapping": up["mapping"]}).json()
    assert len(done["created"]) == 1 and not done["rejected"], done
    alloc = next(a for a in client.get("/v1/org/allocations").json() if a["period"] == "2031-01")
    assert (alloc["unit_id"], alloc["amount_minor"]) == (unit["unit_id"], 1_250_000)
    up = client.post("/v1/sources/uploads?entity=allocation&filename=alloc.csv", content=csv.replace(b"12,500.00", b"13,000.00")).json()
    again = client.post(f"/v1/sources/uploads/{up['upload_id']}/commit", json={"mapping": up["mapping"]}).json()
    assert again["updated"] == [alloc["alloc_id"]] and not again["created"]
