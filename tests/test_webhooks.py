"""Airwallex webhooks: only signed, fresh, first deliveries are accepted, and the API, not the event, is believed."""
import asyncio
import json
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from venthora import webhooks

SECRET = "whsec_test_only"


def _event(eid="evt_1", name="payout.transfer.paid", obj=None):
    obj = obj if obj is not None else {"id": "tr_1", "status": "PAID", "amount": 3000, "currency": "USD"}
    return json.dumps({"id": eid, "name": name, "account_id": "acc", "data": {"object": obj},
                       "created_at": "2026-10-29T01:00:00+0000", "version": "2025-02-14"}).encode()


def _headers(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict:
    ts = str(ts if ts is not None else int(time.time() * 1000))
    return {"x-timestamp": ts, "x-signature": webhooks.sign(secret, ts, body), "content-type": "application/json"}


def test_verify_rules():
    body = _event()
    ts = "1700000000000"
    good = webhooks.sign(SECRET, ts, body)
    now = 1700000000000
    assert webhooks.verify(SECRET, ts, body, good, now_ms=now) is None
    assert webhooks.verify(SECRET, ts, body, good.upper(), now_ms=now) is None
    assert webhooks.verify("", ts, body, good, now_ms=now) == "webhook secret not configured"
    assert webhooks.verify(SECRET, ts, body + b" ", good, now_ms=now) == "signature does not match"
    assert webhooks.verify(SECRET, ts, body, good, now_ms=now + 301_000) == "x-timestamp outside the replay window"
    assert webhooks.verify(SECRET, None, body, good, now_ms=now) == "missing x-timestamp or x-signature"
    assert webhooks.verify(SECRET, "abc", body, good, now_ms=now) == "x-timestamp is not a number"


def test_parse_kinds():
    assert webhooks.parse(_event(name="deposit.settled", obj={"id": "dep_1", "status": "SETTLED"}))["kind"] == "deposit"
    assert webhooks.parse(_event(name="conversion.settled", obj={"conversion_id": "cv_1"}))["object_id"] == "cv_1"
    assert webhooks.parse(_event(name="payment_intent.succeeded"))["kind"] == "other"


def test_endpoint_refuses_and_dedupes(monkeypatch):
    from venthora.api import app
    with TestClient(app) as client:
        body = _event()
        assert client.post("/webhooks/airwallex", content=body, headers=_headers(body)).status_code == 503
        monkeypatch.setenv("AWX_WEBHOOK_SECRET", SECRET)
        assert client.post("/webhooks/airwallex", content=body, headers=_headers(body, secret="wrong")).status_code == 400
        stale = int(time.time() * 1000) - 600_000
        assert client.post("/webhooks/airwallex", content=body, headers=_headers(body, ts=stale)).status_code == 400
        first = client.post("/webhooks/airwallex", content=body, headers=_headers(body))
        again = client.post("/webhooks/airwallex", content=body, headers=_headers(body))
        assert first.json() == {"received": True, "duplicate": False}
        assert again.json() == {"received": True, "duplicate": True}
        rows = client.get("/v1/webhooks/events").json()
        assert len(rows) == 1 and rows[0]["event_id"] == "evt_1"
        # no Airwallex credentials in tests: the claim is stored but never shown as confirmed
        assert rows[0]["state"] == "UNCONFIRMED" and rows[0]["confirmed_status"] is None
        status = client.get("/v1/webhooks/status").json()
        assert status["secret_configured"] is True and status["counts"] == {"UNCONFIRMED": 1}
        assert SECRET not in json.dumps(status)


class _Bank:
    configured = True

    def __init__(self, transfer_status="PROCESSING", deposits=()):
        self.transfer_status, self.deposits = transfer_status, list(deposits)

    async def get_transfer(self, tid):
        return SimpleNamespace(status=self.transfer_status)

    async def get_conversion(self, cid):
        return SimpleNamespace(status="SETTLED")

    async def list_deposits(self):
        return self.deposits


def _confirm(tmp_path, body, bank):
    store = webhooks.WebhookStore(str(tmp_path / "w.db"))
    ev = webhooks.parse(body)
    assert store.record(ev)
    return asyncio.run(webhooks.confirm_event(store, bank, ev))


def test_api_wins_over_the_event(tmp_path):
    row = _confirm(tmp_path, _event(), _Bank(transfer_status="PROCESSING"))
    assert row["state"] == "MISMATCH" and row["confirmed_status"] == "PROCESSING"
    assert "the API wins" in row["note"]


def test_confirmed_deposit_and_unknown_record(tmp_path):
    body = _event(eid="evt_d", name="deposit.settled", obj={"id": "dep_9", "status": "SETTLED", "amount": 10000})
    row = _confirm(tmp_path, body, _Bank(deposits=[{"id": "dep_9", "status": "SETTLED"}]))
    assert row["state"] == "CONFIRMED" and row["confirmed_status"] == "SETTLED"
    body = _event(eid="evt_x", name="deposit.settled", obj={"id": "dep_404", "status": "SETTLED"})
    row = _confirm(tmp_path / "x", body, _Bank(deposits=[]))
    assert row["state"] == "UNCONFIRMED" and row["note"] == "Airwallex has no such record"


def test_hub_fans_out():
    hub = webhooks.Hub()
    q = hub.subscribe()
    hub.publish("webhook", {"event_id": "e"})
    assert q.get_nowait() == {"kind": "webhook", "data": {"event_id": "e"}}
    hub.unsubscribe(q)
    assert hub.listeners == 0


def test_events_left_unconfirmed_are_reread_at_startup(tmp_path):
    store = webhooks.WebhookStore(str(tmp_path / "w.db"))
    store.record(webhooks.parse(_event(eid="evt_left")))  # acknowledged, then the server stopped
    pushed = []
    n = asyncio.run(webhooks.confirm_pending(store, _Bank(transfer_status="PAID"), lambda k, d: pushed.append(d)))
    assert n == 1 and store.get("evt_left")["state"] == "CONFIRMED" and pushed[0]["confirmed_status"] == "PAID"
    assert asyncio.run(webhooks.confirm_pending(store, _Bank())) == 0
