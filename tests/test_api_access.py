# ruff: noqa: E501
"""API access for other applications: a key reaches only its scopes, acts with its own role, cannot manage keys,
and the gateway forwards the API only for requests that carry one. Every endpoint is in the catalogue."""
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteClient

from venthora import apiaccess
from venthora.webhook_gateway import create_app


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _key(client, scopes, role="auditor", **kw):
    r = client.post("/v1/developer/keys", json={"name": "erp-bridge", "scopes": scopes, "role": role} | kw)
    assert r.status_code == 200, r.text
    return r.json()


def test_scopes_roles_and_revocation(client):
    k = _key(client, ["organisation:read", "payables:read"])
    token = k["key"]
    assert token.startswith(f"vth_{k['key_id']}_") and k["hint"].endswith(token[-4:])
    assert all("key" not in x and "hash" not in x for x in client.get("/v1/developer/keys").json())  # the key is shown once
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/org/units", headers=h).status_code == 200
    assert client.get("/v1/payees", headers={"X-API-Key": token}).status_code == 200
    r = client.get("/v1/treasury/position", headers=h)
    assert r.status_code == 403 and "treasury:read" in r.json()["detail"]
    r = client.post("/v1/org/units?role=admin&by=mallory", headers=h, json={"unit_id": "x", "name": "X", "kind": "HQ", "currency": "USD"})
    assert r.status_code == 403 and "organisation:write" in r.json()["detail"]
    assert client.get("/v1/org/units", headers={"Authorization": "Bearer vth_nope_123"}).status_code == 401
    assert client.get("/v1/developer/keys", headers=h).status_code == 403  # a key cannot manage keys
    assert client.get("/v1/developer/catalog", headers=h).status_code == 200
    assert client.get("/openapi.json", headers=h).status_code == 200
    assert client.get("/v1/developer/keys").json()[0]["calls"] >= 4
    client.post(f"/v1/developer/keys/{k['key_id']}/revoke")
    assert client.get("/v1/org/units", headers=h).status_code == 401
    assert client.get("/v1/developer/keys").json()[0]["status"] == "revoked"


def test_the_key_role_replaces_what_the_caller_claims(client):
    ro = _key(client, ["organisation:write"], role="auditor")["key"]
    body = {"address": "ap@acme.example", "purpose": "invoices"}
    r = client.post("/v1/org/mailboxes?role=admin&by=mallory", headers={"X-API-Key": ro}, json=body)
    assert r.status_code == 403 and "auditor" in r.json()["detail"]  # admin was claimed; the key says auditor
    rw = _key(client, ["organisation:write"], role="finance_manager")["key"]
    assert client.post("/v1/org/mailboxes?by=mallory", headers={"X-API-Key": rw}, json=body).status_code == 200
    audit = client.get("/api/audit?limit=5").json()
    assert any("api:erp-bridge (finance_manager)" in str(e) for e in audit["events"])


def test_key_requests_are_validated(client):
    assert client.post("/v1/developer/keys", json={"name": "x", "scopes": ["everything:write"]}).status_code == 422
    assert client.post("/v1/developer/keys", json={"name": "x", "scopes": ["sources:read"], "role": "god"}).status_code == 422
    assert client.post("/v1/developer/keys?role=auditor", json={"name": "x", "scopes": ["sources:read"]}).status_code == 403
    k = _key(client, ["sources:read"], expires_days=30)
    assert k["expires_at"] and k["status"] == "active"


def test_every_endpoint_is_in_the_catalogue(client):
    cat = client.get("/v1/developer/catalog").json()
    eps = cat["endpoints"]
    assert len(eps) >= 90 and all(e["area"] for e in eps)
    by = {(e["method"], e["path"]): e for e in eps}
    assert by[("GET", "/v1/treasury/position")]["scope"] == "treasury:read"
    assert by[("POST", "/v1/payees/eval")]["scope"] == "payables:read"  # an evaluation changes nothing
    assert by[("POST", "/v1/executions")]["scope"] == "treasury:write"
    assert by[("POST", "/webhooks/email")]["area"] == "inbound" and by[("POST", "/webhooks/email")]["scope"] is None
    assert by[("POST", "/v1/developer/keys")]["scope"] is None
    assert {a["area"] for a in cat["areas"]} == set(apiaccess.AREAS)


def test_gateway_forwards_the_api_only_with_a_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    up = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler))
    with StarletteClient(create_app(client=up, api=True)) as gw:
        assert gw.get("/v1/org/units").status_code == 401
        assert gw.get("/").status_code == 404 and gw.get("/live").status_code == 404
        r = gw.get("/v1/org/units?limit=3", headers={"Authorization": "Bearer vth_ab_cd", "Cookie": "x=1"})
        assert r.status_code == 200
        assert len(seen) == 1 and seen[0].url.path == "/v1/org/units" and seen[0].url.params["limit"] == "3"
        assert "cookie" not in seen[0].headers and seen[0].headers["authorization"] == "Bearer vth_ab_cd"
    up2 = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler))
    with StarletteClient(create_app(client=up2)) as gw:  # without --api the API stays unreachable
        assert gw.get("/v1/org/units", headers={"X-API-Key": "vth_ab_cd"}).status_code == 404
