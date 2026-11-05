# ruff: noqa: E501
"""Platform Setup: Airwallex Cards and Card Authorization (remote authorization from the mandate), Visa Agent
Identity (TAP signatures) and Visa Intelligent Commerce (X-Pay, the purchase instruction)."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from venthora import cards, visa_tap, visa_vic

FCO = "role=super_admin&by=Rosa%20Lindqvist"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _card(org, **kw):
    rec = {"_id": "card-1", "payee_id": "ben-cloudloom", "payee": "Cloudloom Software LLC", "masked": "************4242", "brand": "VISA",
           "per_tx_minor": 50_000, "monthly_minor": 80_000, "currency": "USD", "merchant": "Cloudloom", "mcc": [], "frozen": False} | kw
    org._put("card", rec, "t")
    return rec


def _req(amount=300.0, merchant="CLOUDLOOM SOFTWARE", **kw):
    return {"version": 2, "card_id": "card-1", "card_transaction_event_id": f"evt-{time.time_ns()}", "card_transaction_lifecycle_id": f"life-{time.time_ns()}",
            "transaction_type": "AUTHORIZATION", "transaction_amount": amount, "transaction_currency": "USD", "merchant": {"name": merchant, "category_code": "5734"}} | kw


def test_the_signature_is_the_nonce_signed_with_the_shared_secret():
    nonce = cards.new_nonce()
    assert cards.verify(nonce, cards.sign(nonce, "s3cret-value"), "s3cret-value") == (True, "signed by Airwallex")
    assert cards.verify(nonce, cards.sign(nonce, "other"), "s3cret-value")[0] is False
    old = f"{int(time.time() * 1000) - 10 * 60 * 1000}.abc"
    assert cards.verify(old, cards.sign(old, "s3cret-value"), "s3cret-value") == (False, "the request is too old")
    assert cards.verify(nonce, None, "s3cret-value")[0] is False and cards.verify(nonce, "x", "")[0] is False


def test_a_charge_is_decided_from_the_mandate_alone(client):
    org = client.app.state.org
    assert cards.decide(org, _req())[0] == "DECLINED"  # no card yet: not VENTHORA's
    _card(org)
    assert cards.decide(org, _req(300.0)) [0] == "AUTHORIZED"
    assert cards.decide(org, _req(600.0)) == ("DECLINED", "above the card's per-transaction limit of USD 500.00")
    assert "not the card's vendor" in cards.decide(org, _req(merchant="SOMEONE ELSE"))[1]
    first = _req(450.0)
    cards.record_decision(org, first, "AUTHORIZED", "ok", source="test")
    assert "monthly limit" in cards.decide(org, _req(400.0))[1]  # 450 + 400 > 800 this month
    again = dict(first, card_transaction_event_id="evt-retry")
    assert cards.decide(org, again) == ("DECLINED", "this charge was already authorized")
    _card(org, frozen=True)
    assert cards.decide(org, _req(10.0)) == ("DECLINED", "card frozen in VENTHORA")


def test_card_limits_stay_inside_the_mandate_and_the_card_is_visa(client):
    org = client.app.state.org
    ceiling = cards.mandate_ceiling(org)
    with pytest.raises(ValueError, match="single payout limit"):
        cards.check_limits(org, ceiling + 1, ceiling + 1)
    with pytest.raises(ValueError, match="monthly"):
        cards.check_limits(org, 1000, 500)
    body = cards.card_body(cardholder_id="ch-1", payee={"_id": "p1", "name": "Cloudloom"}, per_tx_minor=50_000, monthly_minor=80_000,
                           currency="USD", by="Rosa", mcc=["5734"])
    assert body["brand"] == "VISA" and body["form_factor"] == "VIRTUAL" and body["program"] == {"purpose": "COMMERCIAL"}
    limits = body["authorization_controls"]["transaction_limits"]["limits"]
    assert limits == [{"amount": 500.0, "interval": "PER_TRANSACTION"}, {"amount": 800.0, "interval": "MONTHLY"}]
    assert body["authorization_controls"]["allowed_merchant_categories"] == ["5734"]


def test_airwallex_asks_and_venthora_answers_signed_requests_only(client):
    from venthora import secret_values

    _card(client.app.state.org)
    secret_values.put(cards.SECRET, "shared-secret-for-tests", "t")
    req = _req(120.0)
    raw = json.dumps(req).encode()
    nonce = cards.new_nonce()
    r = client.post("/webhooks/airwallex/remote-auth", content=raw, headers={"content-type": "application/json", "x-nonce": nonce,
                                                                             "x-signature": cards.sign(nonce, "shared-secret-for-tests")})
    assert r.status_code == 200 and r.json() == {"card_transaction_event_id": req["card_transaction_event_id"], "response_status": "AUTHORIZED",
                                                  "status_reason": "inside the mandate: Cloudloom Software LLC, USD 120.00"}
    bad = client.post("/webhooks/airwallex/remote-auth", content=raw, headers={"x-nonce": nonce, "x-signature": "forged"})
    assert bad.status_code == 401
    d = client.get("/v1/card-authorization").json()["decisions"]
    assert d[0]["decision"] == "AUTHORIZED" and d[0]["source"] == "airwallex"
    t = client.post(f"/v1/card-authorization/test?card_id=card-1&{FCO}", json={"amount_minor": 99_900, "currency": "USD"}).json()
    assert t["answer"]["response_status"] == "DECLINED" and t["signature"] == "signed by Airwallex"


def test_the_agent_signs_as_visas_sample_does_and_a_merchant_can_check_it(client):
    with pytest.raises(ValueError, match="no signing key"):
        visa_tap.sign("https://shop.example/checkout")
    info = visa_tap.generate("t")
    pub = info["keys"][0]["public_key"]
    h = visa_tap.sign("https://shop.example/checkout", "agent-payer-auth")
    assert h["Signature-Input"].startswith('sig2=("@authority" "@path"); created=') and 'alg="ed25519"' in h["Signature-Input"]
    assert 'tag="agent-payer-auth"' in h["Signature-Input"] and h["Signature"].startswith("sig2=:") and h["Signature"].endswith(":")
    assert visa_tap.verify("https://shop.example/checkout", h, pub)[0] is True
    assert visa_tap.verify("https://shop.example/other", h, pub) == (False, "the signature does not match the request")
    assert visa_tap.verify("https://shop.example/checkout", h, pub, now=int(time.time()) + 3600)[1] == "the signature is outside its validity window"
    assert client.get("/v1/visa/tap/keys.json").json()["keys"][0]["public_key"] == pub
    t = client.post(f"/v1/visa/tap/sign-test?{FCO}", json={"url": "https://shop.example/checkout", "tag": "agent-browser-auth"}).json()
    assert t["verified"] is True


def test_xpay_token_and_the_purchase_instruction():
    tok = visa_vic.xpay_token("helloworld", "apikey=ABC", "", "secret", ts=1_700_000_000)
    assert tok.startswith("xv2:1700000000:") and len(tok.split(":")[2]) == 64
    assert tok == visa_vic.xpay_token("helloworld", "apikey=ABC", "", "secret", ts=1_700_000_000)
    pi = visa_vic.purchase_instruction(payee="Cloudloom", amount_minor=45_000, currency="USD", mcc="5734", category="Software",
                                       description="Annual plan", recurring=True)
    m = pi["mandates"][0]
    assert m["declineThreshold"] == {"amount": "450.00", "currencyCode": "USD"} and m["preferredMerchantName"] == "Cloudloom"
    assert m["recurringFrequency"] == "MONTHLY" and m["merchantCategoryCode"] == "5734" and pi["consumerId"].startswith("<")


def test_the_gateway_lets_through_the_card_decision_and_the_agent_keys_only():
    from venthora.webhook_gateway import AGENT_KEYS_PATH, REMOTE_AUTH_PATH, create_app

    seen = []

    def upstream(req: httpx.Request):
        seen.append((req.method, req.url.path, dict(req.headers)))
        return httpx.Response(200, json={"ok": True})

    app = create_app(client=httpx.AsyncClient(base_url="http://up", transport=httpx.MockTransport(upstream)))
    with TestClient(app) as g:
        assert g.post(REMOTE_AUTH_PATH, content=b"{}", headers={"x-nonce": "n", "x-signature": "s", "cookie": "no"}).status_code == 200
        assert g.get(AGENT_KEYS_PATH).status_code == 200
        assert g.get("/v1/cards").status_code == 404
    assert seen[0][1] == REMOTE_AUTH_PATH and "cookie" not in seen[0][2] and seen[0][2]["x-nonce"] == "n"
    assert seen[1][:2] == ("GET", "/v1/visa/tap/keys.json")
