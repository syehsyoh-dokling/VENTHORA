"""Disconnect keeps a connection and its key but stops every read and write; Connect from the ERPNext template with the
key fields left blank (masked) re-enables it with the saved key."""
import pytest
from fastapi.testclient import TestClient


from venthora.connectors import ConnectorStore, erpnext_connector

FM = "role=finance_manager&by=test"


@pytest.fixture
def client():
    from venthora.api import app

    with TestClient(app) as c:
        yield c


def test_disconnect_keeps_the_key_refuses_sync_and_connect_turns_it_back_on(client, monkeypatch):
    c = erpnext_connector("https://example.frappe.cloud")
    c.auth.secret = "token k:s"
    ConnectorStore().save(c)

    r = client.post(f"/v1/sources/connectors/erpnext/disconnect?{FM}")
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["auth"]["secret_set"]
    assert ConnectorStore().get_raw("erpnext")["_secret"] == "token k:s"  # the key is kept
    assert client.post(f"/v1/sources/connectors/erpnext/sync?dry_run=true&{FM}").status_code == 409

    import httpx

    async def no_network(*a, **k):
        raise httpx.ConnectError("offline in tests")
    monkeypatch.setattr(httpx.AsyncClient, "get", no_network)
    r = client.post(f"/v1/sources/connectors/template/erpnext?{FM}", json={"site_url": "https://example.frappe.cloud", "api_key": "", "api_secret": ""})
    assert r.status_code == 200
    raw = ConnectorStore().get_raw("erpnext")
    assert raw["enabled"] is True and raw["_secret"] == "token k:s"  # connected again with the saved key, nothing retyped
