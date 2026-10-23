"""API, audit chain, Airwallex client and ECB stress."""
import asyncio
import sqlite3

import httpx
from fastapi.testclient import TestClient

from venthora.api import app
from venthora.audit import AuditStore


def test_api_plans_scenario_and_audits_it():
    with TestClient(app) as client:
        ids = [s["id"] for s in client.get("/api/scenarios").json()]
        assert {"kit01_baseline", "kit01_receipt_delayed"} <= set(ids)
        res = client.post("/api/scenarios/kit01_baseline/plan")
        assert res.status_code == 200 and res.json()["decision"] == "APPROVED"
        assert client.post("/api/scenarios/..%2Fsecrets/plan").status_code == 404
        log = client.get("/api/audit").json()
        assert log["integrity"]["verified"] and log["integrity"]["checked"] == 1
        assert client.get("/api/airwallex/balances").status_code == 503


def test_execution_api_refuses_without_sandbox_credentials():
    with TestClient(app) as client:
        state = client.get("/api/scenarios/kit01_baseline").json()["state"]
        plan = client.post("/v1/plans", json=state).json()
        res = client.post("/v1/executions", json={"state": state, "plan_hash": plan["plan_hash"]})
        assert res.status_code == 200 and res.json()["status"] == "REFUSED"
        assert client.post("/api/sandbox/deposits", json={"amount_minor": 100}).status_code == 400
        assert client.get("/v1/executions/unknown").status_code == 404


def test_audit_chain_detects_tampering(tmp_path):
    store = AuditStore(str(tmp_path / "chain.db"))
    store.append("plan", {"amount_minor": 1})
    store.append("plan", {"amount_minor": 2})
    assert store.verify()["verified"]
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE events SET payload_json='{}' WHERE seq=1")
    assert store.verify() == {"verified": False, "checked": 0, "broken_seq": 1}


def test_airwallex_client_authenticates_once_and_parses_minor_units(monkeypatch):
    from venthora.gateways.airwallex import Airwallex

    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "test-client")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "test-key")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/v1/authentication/login":
            assert request.headers["x-client-id"] == "test-client"
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        assert request.headers["authorization"] == "Bearer t"
        return httpx.Response(200, json=[{"currency": "USD", "available_amount": 20000.5},
                                         {"currency": "EUR", "available_amount": 0}])

    async def go():
        client = httpx.AsyncClient(base_url="https://sandbox.test", transport=httpx.MockTransport(handler))
        bank = Airwallex(client=client)
        first, second = await bank.balances_minor(), await bank.balances_minor()
        await bank.close()
        return first, second

    first, second = asyncio.run(go())
    assert first == second == {"USD": 2_000_050, "EUR": 0}
    assert calls.count("/api/v1/authentication/login") == 1


def test_ecb_stress_from_imported_file(tmp_path):
    from venthora.data.ecb import fx_stress_ppm

    path = tmp_path / "ecb.csv"
    lines = ["Date,USD,GBP,"]
    for i in range(60):  # test fixture only; real data comes from scripts/import_ecb.py
        lines.append(f"2026-0{1 + i // 28}-{1 + i % 28:02d},{1.08 + 0.001 * (i % 7):.4f},0.8600,")
    path.write_text("\n".join(lines), encoding="utf-8")
    result = fx_stress_ppm("EUR", "USD", path=path)
    assert result["available"] and result["stress_ppm"] > 0 and result["observations"] == 60
    assert fx_stress_ppm("EUR", "USD", path=tmp_path / "none.csv")["available"] is False
