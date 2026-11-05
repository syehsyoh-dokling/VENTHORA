# ruff: noqa: E501
"""Procurement process: a purchase request is sourced (demo directory without a searching model), the RFQ goes to the
chosen vendors with one form link each, every quotation is one row, the cheapest that meets every specification and the
locked brand is recommended, and a person other than the requester awards it (another one only with a reason): the
vendor is registered, the PO raised within the approved estimate, the PR ordered, the PO and regrets queued."""
import pytest
from fastapi.testclient import TestClient

FM = "role=finance_manager&by=Dana%20Finch"
REQ = "role=country_finance_lead&by=Amira%20Shah"
PR = {"title": "HP 206A toner cartridges", "requested_by": "Amira Shah", "currency": "USD", "estimated_minor": 100_000,
      "charge_code": "US-200.OPS.6700", "needed_by": "2026-12-01", "quantity": 10, "unit": "pcs", "brand": "HP",
      "specs": "Original HP 206A black; Yield at least 1,350 pages; Delivered to the US office"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("VENTHORA_PROCUREMENT_DIR", str(tmp_path / "procurement"))
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _tokens(pr_number):
    from venthora.organisation import OrgStore

    return {c["cand_id"]: c["token"] for c in OrgStore().get("rfq", pr_number)["candidates"] if c.get("token")}


def test_rfq_quotes_recommendation_and_award_raise_the_po(client):
    pr = client.post(f"/v1/org/purchase-requests?{FM}", json=PR).json()
    n = pr["pr_number"]
    assert pr["specs"] == ["Original HP 206A black", "Yield at least 1,350 pages", "Delivered to the US office"] and pr["brand"] == "HP"
    assert any(r["pr_number"] == n for r in client.get("/v1/procurement/board").json()["requests"])

    src = client.post(f"/v1/procurement/{n}/source?{FM}")
    assert src.status_code == 200, src.text
    demo = [c for c in src.json()["candidates"] if c.get("demo")]
    assert len(demo) >= 4 and all("token" not in c for c in src.json()["candidates"])
    assert all(c["email"].endswith(".example") for c in demo)

    ids = [c["cand_id"] for c in demo[:4]]
    inv = client.post(f"/v1/procurement/{n}/invite?{FM}", json={"cand_ids": ids})
    assert inv.status_code == 200 and inv.json()["sent"] == ids
    from venthora.organisation import OrgStore

    mails = [m for m in OrgStore().list("outbox") if m["kind"] == "rfq_invite" and m["ref"] == n]
    tok = _tokens(n)
    assert len(mails) == 4 and all(f"/rfq/{tok[c]}" in next(m["body"] for m in mails if m["to"] == [d["email"]]) for c, d in zip(ids, demo[:4], strict=True))
    assert client.get(f"/v1/procurement/{n}/form-link/{ids[0]}").json()["url"] == f"/rfq/{tok[ids[0]]}"

    # the vendor's own form: what is asked, then a quotation; a bad link is refused
    assert client.get("/rfq/nope/data").status_code == 404
    data = client.get(f"/rfq/{tok[ids[0]]}/data").json()
    assert data["pr"]["brand"] == "HP" and len(data["pr"]["specs"]) == 3 and data["quote"] is None
    bad = client.post(f"/rfq/{tok[ids[0]]}", json={"vendor": {"name": "X"}})
    assert bad.status_code == 409 and "required" in bad.json()["detail"]

    sub = client.post(f"/v1/procurement/{n}/demo-replies?{FM}")
    assert sub.status_code == 200 and len(sub.json()["submitted"]) == 4
    b = next(x for x in client.get("/v1/procurement/board").json()["board"] if x["pr"]["pr_number"] == n)
    assert len(b["quotes"]) == 4  # one vendor, one quotation, one row
    a = b["assessment"]
    rows = {r["cand_id"]: r for r in a["rows"]}
    eligible = [q for q in b["quotes"] if rows[q["cand_id"]]["eligible"]]
    assert a["recommended"] == min(eligible, key=lambda q: q["total_minor"])["cand_id"]
    off_spec = next(q for q in b["quotes"] if not all(s["ok"] for s in q["specs"]))
    other_brand = next(q for q in b["quotes"] if q["brand"] != "HP")
    assert not rows[off_spec["cand_id"]]["eligible"] and "does not meet" in rows[off_spec["cand_id"]]["why_not"][0]
    assert not rows[other_brand["cand_id"]]["eligible"] and any("brand" in w for w in rows[other_brand["cand_id"]]["why_not"])
    assert client.get(f"/v1/procurement/{n}/file/{a['recommended']}").status_code == 200
    assert client.post(f"/v1/procurement/{n}/review").json()["basis"] == "rules"  # no model in tests

    # the award: only on an approved PR, never by the requester, another quotation only with a reason
    rec = a["recommended"]
    assert "approve the purchase request" in client.post(f"/v1/procurement/{n}/award?{FM}", json={"cand_id": rec}).json()["detail"]
    assert client.post(f"/v1/purchase-requests/{n}/decision?{FM}", json={"decision": "approve"}).status_code == 200
    assert "requester" in client.post(f"/v1/procurement/{n}/award?{REQ}", json={"cand_id": rec}).json()["detail"]
    other = next(q["cand_id"] for q in eligible if q["cand_id"] != rec) if len(eligible) > 1 else other_brand["cand_id"]
    assert "reason" in client.post(f"/v1/procurement/{n}/award?{FM}", json={"cand_id": other}).json()["detail"]
    r = client.post(f"/v1/procurement/{n}/award?{FM}", json={"cand_id": rec})
    assert r.status_code == 200, r.text
    aw = r.json()
    last = max(int(p["po_id"].rsplit("-", 1)[1]) for p in OrgStore().list("po") if p["po_id"].startswith(aw["po_id"][:8]) and p["po_id"] != aw["po_id"])
    assert aw["po_id"] == f"{aw['po_id'][:8]}{last + 1:04d}"  # the next PO number after the last one
    assert not aw["outside_recommendation"] and aw["po"]["pr_number"] == n
    prs = {p["pr_number"]: p for p in client.get("/v1/purchase-requests").json()}
    assert prs[n]["status"] == "ORDERED" and prs[n]["po_id"] == aw["po_id"]
    from venthora.masterdata import MasterStore

    assert MasterStore().get("vendor", aw["vendor_id"])["contacts"]  # registered as a vendor (NEW until the call-back)
    kinds = [m["kind"] for m in OrgStore().list("outbox") if m["ref"] in (n, aw["po_id"])]
    assert kinds.count("po_issued") == 1 and kinds.count("rfq_regret") == 3
    assert client.post(f"/v1/procurement/{n}/award?{FM}", json={"cand_id": rec}).status_code == 409  # once
    assert client.get(f"/rfq/{tok[ids[0]]}/data").json()["closed"] is True


def test_a_real_vendor_link_is_never_shown_and_the_gateway_only_passes_token_paths(client):
    pr = client.post(f"/v1/org/purchase-requests?{FM}", json=PR | {"brand": ""}).json()
    n = pr["pr_number"]
    assert client.post(f"/v1/procurement/{n}/vendors?{FM}", json={"name": "Real Supplier Ltd", "email": "sales@real-supplier.test"}).status_code == 200
    assert client.post(f"/v1/procurement/{n}/invite?{FM}", json={"cand_ids": ["real-supplier-ltd"]}).status_code == 200
    assert client.get(f"/v1/procurement/{n}/form-link/real-supplier-ltd").status_code == 403

    import httpx
    from starlette.testclient import TestClient as StarletteClient

    from venthora.webhook_gateway import create_app

    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"ok": True})

    up = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler))
    with StarletteClient(create_app(client=up)) as gw:
        assert gw.get("/rfq/short").status_code == 404
        good = "A" * 24
        assert gw.get(f"/rfq/{good}").status_code == 200 and gw.get(f"/rfq/{good}/data").status_code == 200
        assert gw.post(f"/rfq/{good}", json={"x": 1}).status_code == 200
        assert gw.get("/v1/procurement/board").status_code == 404  # the board stays private
    assert seen == [("GET", f"/rfq/{good}"), ("GET", f"/rfq/{good}/data"), ("POST", f"/rfq/{good}")]
