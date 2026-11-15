"""A Midtrans payment notification reaches the company whose server key signed it, through the platform's front door."""
import json

import httpx
from starlette.testclient import TestClient

from venthora import saas, saas_gateway


def test_a_midtrans_notification_goes_to_the_company_that_accepts_its_signature(tmp_path, monkeypatch):
    reg = saas.Registry(tmp_path)
    a = reg.create(name="Alpha Pte Ltd", fco_name="Ana Alpha", fco_email="ana@alpha.test", by="t")
    b = reg.create(name="Beta Pte Ltd", fco_name="Ben Beta", fco_email="ben@beta.test", by="t")
    seen: list[int] = []

    def company(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.port)
        if request.url.path != "/webhooks/midtrans":
            return httpx.Response(404)
        if request.url.port == b["port"] and json.loads(request.content)["signature_key"] == "beta-signs":
            return httpx.Response(200, json={"received": True})
        return httpx.Response(401, json={"detail": "signature missing or wrong"})

    real = httpx.AsyncClient
    transport = httpx.MockTransport(company)
    monkeypatch.setattr(saas_gateway.httpx, "AsyncClient", lambda *args, **kw: real(*args, **(kw | {"transport": transport})))
    with TestClient(saas_gateway.create_app(tmp_path, spawn=False)) as gw:
        ok = gw.post("/webhooks/midtrans", json={"order_id": "o-1", "signature_key": "beta-signs"})
        assert ok.status_code == 200 and ok.json() == {"received": True}
        assert b["port"] in seen and set(seen) <= {a["port"], b["port"]}
        forged = gw.post("/webhooks/midtrans", json={"order_id": "o-1", "signature_key": "forged"})
        assert forged.status_code == 401
