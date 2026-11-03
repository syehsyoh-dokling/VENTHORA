# ruff: noqa: E501
"""A finance application connects with one line; every item is read and sent back unless unticked; what is sent
back goes once per change, with an Idempotency-Key, and a failure is retried on the next run."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from venthora import connectors
from venthora.sync_catalog import PULL, PUSH, parse_connection


@pytest.fixture
def client(monkeypatch):
    from venthora.api import app

    real_fetch, real_send = connectors.fetch, connectors.send

    def local():  # the stand-in ERP is served by the app itself
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    async def fetch(raw, entity, client=None):
        async with local() as c:
            return await real_fetch(raw, entity, client=c)

    async def send(raw, item, records, store, dry_run=False, client=None, limit=200):
        async with local() as c:
            return await real_send(raw, item, records, store, dry_run=dry_run, client=c, limit=limit)
    monkeypatch.setattr(connectors, "fetch", fetch)
    monkeypatch.setattr(connectors, "send", send)
    with TestClient(app) as c:
        yield c


def test_one_line_connection():
    assert parse_connection("https://erp.example.com/api/ Bearer abc.def") == ("https://erp.example.com/api", {"type": "bearer", "secret": "abc.def"})
    assert parse_connection("https://erp.example.com/api X-Api-Key: k123") == (
        "https://erp.example.com/api", {"type": "header", "header_name": "X-Api-Key", "secret": "k123"})
    assert parse_connection("https://erp.example.com tok") == ("https://erp.example.com", {"type": "bearer", "secret": "tok"})
    assert parse_connection("https://ann:s3cret@erp.example.com:8443/v2") == (
        "https://erp.example.com:8443/v2", {"type": "basic", "username": "ann", "secret": "s3cret"})
    assert parse_connection("http://localhost:9000") == ("http://localhost:9000", {"type": "none"})
    for bad in ("", "erp.example.com Bearer x", "https://erp.example.com Bearer two words"):
        with pytest.raises(ValueError):
            parse_connection(bad)


def test_connect_ticks_everything_unless_unticked(client):
    cat = client.get("/v1/sources/catalog").json()
    assert [i["key"] for i in cat["pull"]] == list(PULL) and [i["key"] for i in cat["push"]] == list(PUSH)
    assert all(i["why"] and i["fields"] for i in cat["pull"] + cat["push"])
    c = client.post("/v1/sources/connectors", json={"name": "Acme Books", "connection": "https://books.acme.example/api X-Api-Key: k-123456789"}).json()
    assert c["connector_id"] == "acme-books" and c["schedule_minutes"] == 1  # realtime by default
    assert c["auth"] == {"type": "header", "username": "", "header_name": "X-Api-Key", "secret_set": True, "secret_hint": "…6789"}
    assert all(e["enabled"] for e in c["entities"].values()) and set(c["entities"]) == set(PULL)
    assert all(p["enabled"] for p in c["push"].values()) and set(c["push"]) == set(PUSH)
    assert c["entities"]["vendor"]["mapping"]["bank.iban"] == "bank.iban"  # VENTHORA's names until mapped
    c2 = client.post("/v1/sources/connectors", json={"name": "Acme Books", "connection": "https://books.acme.example/api tok",
                                                     "pull": ["vendor", "bill"], "push": ["payment"], "schedule_minutes": 0}).json()
    assert c2["connector_id"] == "acme-books-2"
    assert {k for k, e in c2["entities"].items() if e["enabled"]} == {"vendor", "bill"}
    assert {k for k, p in c2["push"].items() if p["enabled"]} == {"payment"}
    assert client.post("/v1/sources/connectors", json={"name": "x", "connection": "books.acme.example key"}).status_code == 422
    assert client.post("/v1/sources/connectors", json={"name": "x", "connection": "https://a.example k", "push": ["everything"]}).status_code == 422
    assert client.post("/v1/sources/connectors?role=auditor", json={"name": "x", "connection": "https://a.example k"}).status_code == 403
    assert "k-123456789" not in str(client.get("/v1/sources/connectors").json())  # the key is write-only


def test_send_back_to_the_demo_erp_once_per_change(client):
    client.get("/v1/sources/connectors")
    client.post("/v1/sources/connectors/legacy-erp-demo/sync")  # POs, bills and budget arrive first
    dry = client.post("/v1/sources/connectors/legacy-erp-demo/push?dry_run=true").json()["reports"]
    assert set(dry) == set(PUSH) and sum(r["would_send"] for r in dry.values()) > 0
    first = client.post("/v1/sources/connectors/legacy-erp-demo/push").json()["reports"]
    sent = sum(r["sent"] for r in first.values())
    assert sent == sum(r["would_send"] for r in dry.values()) and not any(r["failed"] for r in first.values())
    got = client.get("/demo/legacy-erp-inbox?limit=500").json()
    assert len(got) == sent and all(g["idempotency_key"].startswith(f"venthora-{g['item']}-") for g in got)
    assert {"type", "id", "data"} <= set(got[0]["body"])
    po = next(g for g in got if g["item"] == "po_balance")["body"]["data"]
    assert set(po) == set(PUSH["po_balance"][3])
    again = client.post("/v1/sources/connectors/legacy-erp-demo/push").json()["reports"]
    assert sum(r["sent"] for r in again.values()) == 0  # nothing changed, nothing sent twice
    # unticking an item stops it
    c = client.get("/v1/sources/connectors").json()[0]
    c["push"]["po_balance"]["enabled"] = False
    client.put("/v1/sources/connectors/legacy-erp-demo", json={k: c[k] for k in ("name", "base_url", "entities", "push", "schedule_minutes")}
               | {"auth": {"type": c["auth"]["type"], "header_name": c["auth"]["header_name"], "secret": None}})
    assert "po_balance" not in client.post("/v1/sources/connectors/legacy-erp-demo/push?dry_run=true").json()["reports"]
    assert client.get("/v1/sources/connectors").json()[0]["auth"]["secret_set"] is True  # the key survived the edit


def test_a_failed_send_is_retried(tmp_path):
    store = connectors.ConnectorStore(str(tmp_path / "m.db"))
    raw = {"connector_id": "x", "base_url": "https://books.example", "auth": {"type": "bearer"}, "_secret": "t",
           "push": {"payment": {"enabled": True, "path": "payments", "method": "POST"}}}
    answers = [500, 200]
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(answers.pop(0) if answers else 200, json={})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await connectors.send(raw, "payment", [("t1", {"amount_minor": 100})], store, client=c)
    assert asyncio.run(run())["failed"] == 1
    assert asyncio.run(run())["sent"] == 1  # the next run sends it again
    assert asyncio.run(run())["unchanged"] == 1 and len(seen) == 2
    assert seen[0].headers["authorization"] == "Bearer t" and seen[0].headers["idempotency-key"] == seen[1].headers["idempotency-key"]
