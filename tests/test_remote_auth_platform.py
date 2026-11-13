# ruff: noqa: E501
"""Card remote authorization on SaaS: for cards of the companies' connected accounts Airwallex signs with the PLATFORM's
secret. The platform checks it once and asks each company; the one that issued the card answers, the others say 404.
A company accepts such a request only with the platform's token, and never answers for a card it did not issue."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteClient

from venthora import cards, saas

TOKEN = "platform-token-for-tests"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VENTHORA_PLATFORM_TOKEN", TOKEN)
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _req(card_id: str) -> dict:
    return {"version": 2, "card_transaction_event_id": "evt-1", "card_id": card_id, "transaction_amount": 12.0,
            "transaction_currency": "USD", "merchant": {"name": "CLOUDLOOM SOFTWARE", "category_code": "5734"}}


def test_a_company_answers_a_platform_verified_request_only_for_its_own_card(client):
    org = client.app.state.org
    org._put("card", {"_id": "card-own", "payee": "Cloudloom Software LLC", "payee_id": "p1", "per_tx_minor": 50_000, "monthly_minor": 80_000,
                      "currency": "USD", "merchant": "Cloudloom Software LLC", "mcc": ["5734"], "frozen": False, "status": "ACTIVE"}, "t")
    ok = client.post("/webhooks/airwallex/remote-auth", content=json.dumps(_req("card-own")), headers={"x-venthora-platform": TOKEN})
    assert ok.status_code == 200 and ok.json()["card_transaction_event_id"] == "evt-1" and ok.json()["response_status"] in ("AUTHORIZED", "DECLINED")
    other = client.post("/webhooks/airwallex/remote-auth", content=json.dumps(_req("card-elsewhere")), headers={"x-venthora-platform": TOKEN})
    assert other.status_code == 404  # the platform asks the next company
    forged = client.post("/webhooks/airwallex/remote-auth", content=json.dumps(_req("card-own")), headers={"x-venthora-platform": "guess"})
    assert forged.status_code == 401  # a wrong token is not the platform: the request must carry Airwallex's signature


def test_a_merchant_name_cut_short_by_the_card_network_still_matches_its_vendor_and_only_it():
    assert cards.merchant_matches("Lumen Office Supplies Pte Ltd", "Lumen Office Supplies Pte")  # cut at the network's limit
    assert cards.merchant_matches("Lumen Office Supplies Pte Ltd", "LUMEN OFFICE SUPPLIES SINGAPORE")
    assert cards.merchant_matches("Cloudloom Software LLC", "CLOUDLOOM SOFTWARE")
    assert cards.merchant_matches("PT Kinetic IT Services", "KINETIC IT SERVICES")
    assert not cards.merchant_matches("Lumen Office Supplies Pte Ltd", "Remote auth retest Nusant")
    assert not cards.merchant_matches("Lumen Office Supplies Pte Ltd", "LUMEN")  # too short to trust
    assert not cards.merchant_matches("Lumen Office Supplies Pte Ltd", "Lumen Travel Pte Ltd")


def test_the_platform_checks_the_signature_once_and_routes_to_the_card_owner(tmp_path, monkeypatch):
    from venthora import saas_gateway

    monkeypatch.setenv("AWX_REMOTE_AUTH_SECRET", "platform-secret")
    reg = saas.Registry(tmp_path)
    a = reg.create(name="Alpha Pte Ltd", fco_name="Ana Alpha", fco_email="ana@alpha.test", by="t")
    b = reg.create(name="Beta Pte Ltd", fco_name="Ben Beta", fco_email="ben@beta.test", by="t")
    seen: list[tuple[int, str | None]] = []

    def tenant(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.port, request.headers.get("x-venthora-platform")))
        if request.headers.get("x-venthora-platform") != reg.token():
            return httpx.Response(401, json={"detail": "the signature does not match"})  # the company checks its own secret
        if request.url.port == b["port"] and json.loads(request.content)["card_id"] == "card-of-beta":
            return httpx.Response(200, json=cards.answer(json.loads(request.content), "AUTHORIZED", "inside the mandate"))
        return httpx.Response(404, json={"detail": "not a card this company issued"})

    real = httpx.AsyncClient
    monkeypatch.setattr(saas_gateway.httpx, "AsyncClient", lambda *args, **kw: real(*args, **(kw | {"transport": httpx.MockTransport(tenant)})))
    app = saas_gateway.create_app(tmp_path, spawn=False)
    raw = json.dumps(_req("card-of-beta")).encode()
    nonce = cards.new_nonce()
    with StarletteClient(app) as gw:
        r = gw.post("/webhooks/airwallex/remote-auth", content=raw, headers={"content-type": "application/json", "x-nonce": nonce,
                                                                             "x-signature": cards.sign(nonce, "platform-secret")})
        assert r.status_code == 200 and r.json()["response_status"] == "AUTHORIZED"
        assert {p for p, _ in seen} == {a["port"], b["port"]} and all(tok == reg.token() for _, tok in seen)
        seen.clear()
        nobody = gw.post("/webhooks/airwallex/remote-auth", content=json.dumps(_req("card-unknown")).encode(),
                         headers={"x-nonce": nonce, "x-signature": cards.sign(nonce, "platform-secret")})
        assert nobody.status_code == 200 and nobody.json()["response_status"] == "DECLINED"
        seen.clear()
        unsigned = gw.post("/webhooks/airwallex/remote-auth", content=raw, headers={"x-nonce": nonce, "x-signature": "forged"})
        assert unsigned.status_code in (401, 404) and all(tok is None for _, tok in seen)  # no platform token without a valid signature
