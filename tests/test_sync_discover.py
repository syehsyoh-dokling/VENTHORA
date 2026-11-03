# ruff: noqa: E501
"""One line in, a mapping out: discovery finds each item in the application's OpenAPI document (or by GET on the
usual names), proposes the field mapping by name and AI, writes nothing, and the proposal, once applied, syncs."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from venthora import connectors, sync_discover
from venthora.sync_catalog import PULL


@pytest.fixture
def app_client(monkeypatch):
    from venthora.api import app

    real_fetch, real_discover = connectors.fetch, sync_discover.discover

    def local():  # the stand-in ERP is served by the app itself
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    async def fetch(raw, entity, client=None):
        async with local() as c:
            return await real_fetch(raw, entity, client=c)

    async def discover(raw, model=None, client=None):
        async with local() as c:
            return await real_discover(raw, model=model, client=c)
    monkeypatch.setattr(connectors, "fetch", fetch)
    monkeypatch.setattr(sync_discover, "discover", discover)
    with TestClient(app) as c:
        yield c


def _base(mapping: dict) -> dict:
    return {k: v.split("|")[0] for k, v in mapping.items()}


def test_names_alone_rebuild_the_hand_written_mapping(app_client):
    c = app_client.post("/v1/sources/connectors", json={"name": "Legacy ERP", "connection": "http://testserver/demo/legacy-erp X-Legacy-Key: demo-legacy-key"}).json()
    before = app_client.get("/v1/sources/connectors").json()
    out = app_client.post(f"/v1/sources/connectors/{c['connector_id']}/discover?ai=false").json()
    assert out["openapi"] == "http://testserver/demo/legacy-erp/openapi.json" and out["model"] == "names only"
    hand = connectors.demo_connector("http://testserver").entities
    for ent, em in hand.items():
        prop = out["entities"][ent]
        assert prop["found"] and prop["path"] == em.path and prop["items_path"] == "items", ent
        assert _base(prop["mapping"]) == _base(em.mapping), ent  # the same source field for every VENTHORA field
        assert all(prop["by"][t] == "name match" for t in prop["mapping"])
    v = out["entities"]["vendor"]
    assert v["mapping"]["bank.iban"] == "Bank.IBAN|nospace|upper" and v["mapping"]["contacts"] == "Emails|split"
    assert v["preview"]["bank.iban"].startswith("•••• ")  # the preview masks bank numbers
    assert out["entities"]["bill"]["mapping"]["amount_minor"] == "Amount|major" and out["entities"]["bill"]["mapping"]["due_date"] == "DueDate|date"
    assert all(p["found"] and p["path"] == f"inbox/{k}" for k, p in out["push"].items())
    assert app_client.get("/v1/sources/connectors").json() == before  # nothing saved
    # applied, the proposal syncs like the hand-written one
    raw = connectors.ConnectorStore().get_raw(c["connector_id"])
    ents = {e: {"enabled": True, "path": p["path"], "items_path": p["items_path"], "mapping": p["mapping"]} for e, p in out["entities"].items()}
    body = {"name": raw["name"], "base_url": raw["base_url"], "entities": ents, "schedule_minutes": 0,
            "push": {k: {"enabled": True, "path": p["path"], "method": p["method"]} for k, p in out["push"].items()},
            "auth": {"type": raw["auth"]["type"], "header_name": raw["auth"]["header_name"], "secret": None}}
    assert app_client.put(f"/v1/sources/connectors/{c['connector_id']}", json=body).status_code == 200
    dry = app_client.post(f"/v1/sources/connectors/{c['connector_id']}/sync?dry_run=true").json()["reports"]
    assert all("error" not in r for r in dry.values()) and sum(len(r.get("created", [])) for r in dry.values()) > 0
    assert not dry["vendor"]["rejected"]


def test_without_openapi_the_usual_names_are_tried_with_get_only():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/suppliers":
            return httpx.Response(200, json={"data": {"results": [{"SupplierID": "S-1", "DisplayName": "Acme", "Currency": "usd"}]}})
        return httpx.Response(404, json={})

    raw = {"base_url": "https://books.example/api", "auth": {"type": "bearer"}, "_secret": "t"}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await sync_discover.discover(raw, client=c)
    out = asyncio.run(run())
    v = out["entities"]["vendor"]
    assert out["openapi"] is None and v["found"] and v["path"] == "suppliers" and v["items_path"] == "data.results"
    assert v["mapping"] == {"vendor_id": "SupplierID|lower", "name": "DisplayName", "currency": "Currency|upper"}
    assert v["preview"] == {"vendor_id": "s-1", "name": "Acme", "currency": "USD"}
    assert not out["entities"]["customer"]["found"] and "HTTP 404" in out["entities"]["customer"]["tried"]
    assert {m for m, _ in seen} == {"GET"}  # discovery never writes
    assert all(not p["found"] for p in out["push"].values())  # without a document, write paths stay the defaults


def test_the_ai_fills_what_names_miss_and_sees_no_real_values():
    class Model:
        configured = True
        prompt = ""

        async def complete(self, messages, max_tokens=300):
            Model.prompt = messages[0]["content"]
            return '{"name": "Partner.Label", "currency": "Nope", "registered_phone": "Tel"}'

    fields = {"Ref": "V-9", "Partner.Label": "Acme Ltd", "Tel": "+65 6123 4567", "Mail": "ap@acme.example", "Acct": "123456789"}
    got = asyncio.run(sync_discover.by_ai(Model(), "vendor", fields, {}))
    assert got == {"name": "Partner.Label", "registered_phone": "Tel"}  # "Nope" is not a field of the record: dropped
    assert "123456789" not in Model.prompt and "ap@acme.example" not in Model.prompt and "6123" not in Model.prompt
    assert asyncio.run(sync_discover.by_ai(None, "vendor", fields, {})) == {}


def test_every_item_has_where_to_look():
    assert set(sync_discover.PATH_WORDS) == set(PULL)
    assert sync_discover.find_list([{"a": 1}]) == ("", [{"a": 1}])
    assert sync_discover.find_list({"value": [{"a": 1}]})[0] == "value"
    assert sync_discover.flatten({"A": {"B": 1}, "L": [{"x": 1}], "T": ["a"]}) == {"A.B": 1, "L": [{"x": 1}], "T": ["a"]}


def test_the_ai_is_asked_only_for_gaps_at_once_and_within_a_budget(monkeypatch):
    def handler(request):
        if request.url.path == "/api/suppliers":  # names cover three fields; "Tel" is left for the model
            return httpx.Response(200, json=[{"SupplierID": "S-1", "DisplayName": "Acme", "Currency": "usd", "Tel": "+65 6123 4567"}])
        if request.url.path == "/api/customers":  # nothing left for the model: not asked
            return httpx.Response(200, json=[{"Code": "C-1", "Name": "Beta"}])
        return httpx.Response(404, json={})

    class Model:
        configured, asked = True, []

        async def complete(self, messages, max_tokens=300):
            Model.asked.append(messages[0]["content"])
            await asyncio.sleep(Model.delay)
            return '{"registered_phone": "Tel"}'

    raw = {"base_url": "https://books.example/api", "auth": {"type": "none"}, "_secret": ""}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await sync_discover.discover(raw, model=Model(), client=c)
    Model.delay = 0
    out = asyncio.run(run())
    assert out["ai_asked"] == ["vendor"] and len(Model.asked) == 1 and "Customers" not in Model.asked[0]
    assert out["entities"]["vendor"]["mapping"]["registered_phone"] == "Tel" and out["entities"]["vendor"]["by"]["registered_phone"] == "AI"
    monkeypatch.setattr(sync_discover, "AI_BUDGET_S", 0.05)
    Model.delay = 1
    out = asyncio.run(run())
    assert "registered_phone" not in out["entities"]["vendor"]["mapping"] and "longer than" in out["ai_errors"][0]
    assert out["entities"]["vendor"]["mapping"]["name"] == "DisplayName"  # the names stand
