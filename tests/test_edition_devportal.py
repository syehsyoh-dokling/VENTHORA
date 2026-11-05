# ruff: noqa: E501
"""Edition: demo mode on is the developer view, off is the end-user view. Only a Super admin switches it, a production
server locks it off, and with it off the demo endpoints answer 404. API Catalog: the public developer page lists only
what an API key may call, never the keys, the bank, the AI vault, the agent's authority, the edition or the demo."""
import httpx
import pytest
from fastapi.testclient import TestClient

SA = "role=super_admin&by=Rosa%20Lindqvist"
FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_demo_mode_is_switched_by_a_super_admin_and_closes_the_demo_endpoints(client):
    e = client.get("/v1/edition").json()
    assert e["demo"] is True and e["locked"] is False  # the alpha-sandbox default
    assert client.get("/v1/evidence").status_code == 200
    assert client.put(f"/v1/edition?{FM}", json={"demo": False}).status_code == 403  # a finance manager may not
    off = client.put(f"/v1/edition?{SA}", json={"demo": False})
    assert off.status_code == 200 and off.json()["demo"] is False and off.json()["changed_by"].startswith("Rosa Lindqvist")
    for path in ("/v1/evidence", "/live"):
        assert client.get(path).status_code == 404, path
    for path in ("/v1/procurement/demo-requests?" + SA, "/v1/procurement/PR-1/demo-replies?" + SA, "/v1/cards/c1/simulate?" + SA, "/api/sandbox/deposits"):
        assert client.post(path, json={}).status_code == 404, path
    assert client.get("/v1/procurement/PR-1/form-link/x").status_code == 404
    assert client.get("/v1/procurement/board").status_code == 200  # the real procurement stays
    assert client.put(f"/v1/edition?{SA}", json={"demo": True}).json()["demo"] is True
    assert client.get("/v1/evidence").status_code == 200


def test_a_production_server_keeps_demo_mode_off(client, monkeypatch):
    monkeypatch.setenv("VENTHORA_EDITION", "production")
    e = client.get("/v1/edition").json()
    assert e["demo"] is False and e["locked"] is True
    assert client.put(f"/v1/edition?{SA}", json={"demo": True}).status_code == 403
    assert client.get("/v1/evidence").status_code == 404


def test_the_public_catalog_lists_only_what_a_key_may_call(client):
    c = client.get("/developers/catalog.json").json()
    paths = {e["path"] for e in c["endpoints"]}
    assert "/v1/balances" in paths and "/v1/procurement/board" in paths and all(e["scope"] for e in c["endpoints"])
    for never in ("/v1/developer/keys", "/v1/airwallex/connection", "/v1/settings/ai", "/v1/mandate", "/v1/edition", "/v1/evidence",
                  "/v1/procurement/demo-requests", "/webhooks/airwallex"):
        assert not any(p == never or p.startswith(never + "/") for p in paths), never
    assert not any("/demo" in p or p.endswith("/simulate") for p in paths)
    spec = client.get("/developers/openapi.json")
    assert "attachment" in spec.headers["content-disposition"]
    doc = spec.json()
    assert set(doc["paths"]) == paths and doc["servers"][0]["url"] == c["base_url"] and doc["security"] == [{"bearer": []}]
    pm = client.get("/developers/postman.json").json()
    assert sum(len(f["item"]) for f in pm["item"]) == len(c["endpoints"]) and pm["variable"][0]["key"] == "baseUrl"
    assert "API Catalog" in client.get("/developers").text


def test_the_gateway_passes_the_developer_page_only():
    from starlette.testclient import TestClient as StarletteClient

    from venthora.webhook_gateway import create_app

    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"ok": True}, headers={"content-disposition": "attachment; filename=x.json"})

    up = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler))
    with StarletteClient(create_app(client=up)) as gw:
        assert gw.get("/developers").status_code == 200
        r = gw.get("/developers/openapi.json")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert gw.get("/developers/secret").status_code == 404  # only the four listed paths
        assert gw.get("/v1/developer/catalog").status_code == 404  # the private catalog stays private
    assert "/v1/balances" not in seen and "/v1/developer/catalog" not in seen
