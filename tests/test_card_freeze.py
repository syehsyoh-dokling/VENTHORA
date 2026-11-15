# ruff: noqa: E501
"""Freezing a card revokes the agent's authority on Airwallex too (card_status INACTIVE), and still in VENTHORA when
Airwallex cannot be reached."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

FM = "role=finance_manager&by=Dana%20Finch"
FCO = "role=super_admin&by=Rosa%20Lindqvist"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_freeze_reaches_airwallex_and_unfreeze_restores(client, monkeypatch):
    from venthora import api

    calls = []

    class Bank:
        async def set_card_status(self, card_id, status):
            calls.append((card_id, status))
            return {"card_id": card_id, "card_status": status}

    monkeypatch.setattr(api, "_bank", lambda: Bank())
    org = client.app.state.org
    org._put("card", {"_id": "card-frz-1", "payee": "Cloudly", "currency": "USD", "per_tx_minor": 1000, "monthly_minor": 5000, "frozen": False}, "test")
    r = client.post(f"/v1/cards/card-frz-1/freeze?{FM}", json={"frozen": True})
    assert r.status_code == 200 and r.json()["airwallex"] == "INACTIVE" and org.get("card", "card-frz-1")["frozen"] is True
    r = client.post(f"/v1/cards/card-frz-1/freeze?{FM}", json={"frozen": False})
    assert r.status_code == 403 and "unfreezes" in r.json()["detail"] and org.get("card", "card-frz-1")["frozen"] is True  # a finance manager stops a card; only the FCO lets it spend again
    r = client.post(f"/v1/cards/card-frz-1/freeze?{FCO}", json={"frozen": False})
    assert r.json()["airwallex"] == "ACTIVE" and calls == [("card-frz-1", "INACTIVE"), ("card-frz-1", "ACTIVE")]


def test_freeze_still_holds_in_venthora_without_airwallex(client, monkeypatch):
    from venthora import api

    def no_bank():
        raise HTTPException(503, "Airwallex sandbox credentials are not configured")

    monkeypatch.setattr(api, "_bank", no_bank)
    org = client.app.state.org
    org._put("card", {"_id": "card-frz-2", "payee": "Cloudly", "currency": "USD", "per_tx_minor": 1000, "monthly_minor": 5000, "frozen": False}, "test")
    r = client.post(f"/v1/cards/card-frz-2/freeze?{FM}", json={"frozen": True})
    assert r.status_code == 200 and r.json()["airwallex"].startswith("not updated") and org.get("card", "card-frz-2")["frozen"] is True
