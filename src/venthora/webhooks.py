# ruff: noqa: E501
"""Airwallex webhooks: a doorbell, never the source of truth.

Airwallex pushes an event the moment a deposit, transfer or conversion changes status, so the dashboards
update in seconds instead of waiting for the next poll. VENTHORA treats the push as a hint only:

* signature  - HMAC-SHA256 over ``x-timestamp`` + the raw body, keyed with the endpoint's secret
               (AWX_WEBHOOK_SECRET); a missing or wrong signature, or a stale timestamp, is refused.
* once only  - Airwallex retries for days and may deliver an event twice; the event id is stored and a
               repeat is acknowledged without being processed again.
* re-read    - the status in the body is a claim. The record is fetched again from the Airwallex API and
               only that answer is stored as confirmed; a disagreement is kept and shown.
* no money   - an event never moves money or changes a book. Books change only through the executor and
               the deposit check, which read Airwallex themselves; polling stays as the fallback.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import data_path, env

TOLERANCE_S = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, object_id TEXT,
    claimed_status TEXT, confirmed_status TEXT, state TEXT NOT NULL, note TEXT,
    amount TEXT, currency TEXT, created_at TEXT, received_at TEXT NOT NULL, confirmed_at TEXT);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def secret() -> str:
    return env("AWX_WEBHOOK_SECRET")


def sign(secret_key: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret_key.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()


def verify(secret_key: str, timestamp: str | None, body: bytes, signature: str | None, *,
           now_ms: int | None = None, tolerance_s: int = TOLERANCE_S) -> str | None:
    """None when the delivery is authentic and fresh, otherwise the reason it is refused."""
    if not secret_key:
        return "webhook secret not configured"
    if not timestamp or not signature:
        return "missing x-timestamp or x-signature"
    try:
        ts = int(timestamp)
    except ValueError:
        return "x-timestamp is not a number"
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(now_ms - ts) > tolerance_s * 1000:
        return "x-timestamp outside the replay window"
    if not hmac.compare_digest(sign(secret_key, timestamp, body), signature.strip().lower()):
        return "signature does not match"
    return None


def kind_of(name: str) -> str:
    if name.startswith("deposit."):
        return "deposit"
    if name.startswith(("payout.transfer.", "transfer.")):
        return "transfer"
    if name.startswith("conversion."):
        return "conversion"
    return "other"


def parse(body: bytes) -> dict:
    event = json.loads(body)
    if not isinstance(event, dict) or not event.get("id") or not event.get("name"):
        raise ValueError("an event needs an id and a name")
    obj = ((event.get("data") or {}).get("object") or {}) if isinstance(event.get("data"), dict) else {}
    amount = obj.get("amount", obj.get("buy_amount", obj.get("transfer_amount")))
    return {"event_id": str(event["id"]), "name": str(event["name"]), "kind": kind_of(str(event["name"])),
            "object_id": str(obj.get("id") or obj.get("transfer_id") or obj.get("conversion_id") or "") or None,
            "claimed_status": obj.get("status"), "amount": None if amount is None else str(amount),
            "currency": obj.get("currency") or obj.get("transfer_currency") or obj.get("buy_currency"),
            "created_at": event.get("created_at")}


class WebhookStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_WEBHOOK_DB", "data/venthora_webhooks.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, ev: dict) -> bool:
        """Store a new event; False when the event id was already received (a retry or a duplicate)."""
        with self._lock, self._connect() as c:
            cur = c.execute("INSERT OR IGNORE INTO webhook_events (event_id, name, kind, object_id, claimed_status, state, "
                            "amount, currency, created_at, received_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (ev["event_id"], ev["name"], ev["kind"], ev["object_id"], ev["claimed_status"],
                             "RECEIVED" if ev["kind"] != "other" else "IGNORED", ev["amount"], ev["currency"],
                             ev["created_at"], _now()))
            return cur.rowcount == 1

    def confirm(self, event_id: str, *, state: str, confirmed_status: str | None, note: str) -> dict:
        with self._lock, self._connect() as c:
            c.execute("UPDATE webhook_events SET state = ?, confirmed_status = ?, note = ?, confirmed_at = ? WHERE event_id = ?",
                      (state, confirmed_status, note, _now(), event_id))
        return self.get(event_id) or {}

    def get(self, event_id: str) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT * FROM webhook_events WHERE event_id = ?", (event_id,)).fetchone()
        return dict(r) if r else None

    def list(self, limit: int = 50) -> list[dict]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM webhook_events ORDER BY received_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._connect() as c:
            return {r["state"]: r["n"] for r in c.execute("SELECT state, COUNT(*) AS n FROM webhook_events GROUP BY state")}

    def pending(self) -> list[dict]:
        """Events acknowledged but never re-read (the server stopped in between)."""
        with self._connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM webhook_events WHERE state = 'RECEIVED' ORDER BY received_at")]


async def confirm_pending(store: WebhookStore, bank, publish=None) -> int:
    """At start-up: re-read every event left unconfirmed, so none stays RECEIVED forever."""
    rows = store.pending()
    for row in rows:
        out = await confirm_event(store, bank, row)
        if publish:
            publish("webhook", out)
    return len(rows)


async def authoritative_status(bank, kind: str, object_id: str) -> str | None:
    """The status Airwallex's own API reports for the object (None when it cannot be found)."""
    if kind == "transfer":
        return (await bank.get_transfer(object_id)).status
    if kind == "conversion":
        return (await bank.get_conversion(object_id)).status
    if kind == "deposit":
        return next((d.get("status") for d in await bank.list_deposits() if str(d.get("id")) == object_id), None)
    return None


async def confirm_event(store: WebhookStore, bank, ev: dict) -> dict:
    """Re-read the object from Airwallex and store what the API says, not what the event said."""
    if ev["kind"] == "other":
        return store.confirm(ev["event_id"], state="IGNORED", confirmed_status=None, note="not a deposit, transfer or conversion event")
    if not ev["object_id"]:
        return store.confirm(ev["event_id"], state="UNCONFIRMED", confirmed_status=None, note="the event names no object id")
    if not bank.configured:
        return store.confirm(ev["event_id"], state="UNCONFIRMED", confirmed_status=None,
                             note="Airwallex credentials not configured: the claim was not checked")
    try:
        status = await authoritative_status(bank, ev["kind"], ev["object_id"])
    except Exception as exc:  # noqa: BLE001 - any failure leaves the event unconfirmed; polling still catches the change
        return store.confirm(ev["event_id"], state="UNCONFIRMED", confirmed_status=None, note=f"re-read failed: {exc}")
    if status is None:
        return store.confirm(ev["event_id"], state="UNCONFIRMED", confirmed_status=None, note="Airwallex has no such record")
    claimed = (ev["claimed_status"] or "").upper()
    if claimed and claimed != status.upper():
        return store.confirm(ev["event_id"], state="MISMATCH", confirmed_status=status,
                             note=f"event said {claimed}, the API says {status}: the API wins")
    return store.confirm(ev["event_id"], state="CONFIRMED", confirmed_status=status, note="confirmed by the Airwallex API")


class Hub:
    """In-process fan-out to the dashboards' event streams (server-sent events)."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def publish(self, kind: str, data: dict) -> None:
        for q in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):  # a stalled viewer misses an update, never blocks the webhook
                q.put_nowait({"kind": kind, "data": data})

    @property
    def listeners(self) -> int:
        return len(self._queues)
