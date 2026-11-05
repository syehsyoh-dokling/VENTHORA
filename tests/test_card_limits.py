# ruff: noqa: E501
"""Kit 02: VENTHORA reads the limits Airwallex enforces on a card and compares them with what it issued; a limit raised
or added on Airwallex outside VENTHORA is drift, recorded in the audit chain."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _bank(monkeypatch, live):
    from venthora import api

    class Bank:
        async def card_limits(self, card_id):
            return live

    monkeypatch.setattr(api, "_bank", lambda: Bank())


def test_limits_that_match_are_ok(client, monkeypatch):
    client.app.state.org._put("card", {"_id": "card-lim-1", "payee": "Cloudly", "currency": "USD", "per_tx_minor": 50_000, "monthly_minor": 200_000}, "test")
    _bank(monkeypatch, {"currency": "USD", "limits": [{"amount": 500, "interval": "PER_TRANSACTION", "remaining": 500},
                                                      {"amount": 2000, "interval": "MONTHLY", "remaining": 1250.5}]})
    r = client.get("/v1/cards/card-lim-1/limits").json()
    assert r["ok"] and r["drift"] == []
    assert {x["interval"]: x["remaining_minor"] for x in r["limits"]} == {"PER_TRANSACTION": 50_000, "MONTHLY": 125_050}


def test_a_limit_raised_or_added_on_airwallex_is_drift(client, monkeypatch):
    client.app.state.org._put("card", {"_id": "card-lim-2", "payee": "Cloudly", "currency": "USD", "per_tx_minor": 50_000, "monthly_minor": 200_000}, "test")
    _bank(monkeypatch, {"currency": "USD", "limits": [{"amount": 900, "interval": "PER_TRANSACTION"}, {"amount": 100, "interval": "DAILY"}]})
    r = client.get("/v1/cards/card-lim-2/limits").json()
    assert not r["ok"]
    assert any("Airwallex allows USD 900.00" in d for d in r["drift"])
    assert any("daily limit set on Airwallex" in d for d in r["drift"])
    assert any("monthly limit that Airwallex does not show" in d for d in r["drift"])
    assert client.get("/v1/cards/nope/limits").status_code == 404
