# ruff: noqa: E501
"""Which Airwallex account VENTHORA works on: connected in User management → Airwallex account, or .env.

The client ID and API key a finance manager pastes are tested (one login) before they are kept, stored in this
machine's settings database (data/, git-ignored, like .env), and never returned: reads show the client ID's last
four characters and whether a key is set. Saved credentials win over AIRWALLEX_CLIENT_ID / AIRWALLEX_API_KEY in
.env, which still apply when nothing was saved. VENTHORA executes on the Airwallex sandbox only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .config import data_path, env


class Connection(BaseModel):
    client_id: str = Field(min_length=8, max_length=200)
    api_key: str | None = Field(default=None, max_length=500, description="write-only; None keeps the stored key")
    account_name: str = Field(default="", max_length=200)


class AirwallexStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_SECRETS_DB", "data/venthora_secrets.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS airwallex (id INTEGER PRIMARY KEY CHECK (id = 1), record_json TEXT NOT NULL,"
                      " updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS acting (id INTEGER PRIMARY KEY CHECK (id = 1), account_id TEXT NOT NULL, name TEXT NOT NULL,"
                      " updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS card_account (id INTEGER PRIMARY KEY CHECK (id = 1), account_id TEXT NOT NULL, name TEXT NOT NULL,"
                      " updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def raw(self) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT record_json, updated_at, updated_by FROM airwallex WHERE id = 1").fetchone()
        return {**json.loads(r["record_json"]), "_updated_at": r["updated_at"], "_updated_by": r["updated_by"]} if r else None

    def merged(self, conn: Connection) -> dict:
        old = self.raw() or {}
        key = (conn.api_key or "").strip() or (old.get("api_key") if old.get("client_id") == conn.client_id.strip() else "")
        return {"client_id": conn.client_id.strip(), "api_key": key or "", "account_name": conn.account_name.strip()}

    def save(self, rec: dict, by: str) -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO airwallex VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET record_json = excluded.record_json,"
                      " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                      (json.dumps(rec), datetime.now(UTC).isoformat(timespec="seconds"), by))

    def clear(self) -> None:
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM airwallex")

    # -- acting for an account this platform opened (Airwallex x-on-behalf-of): no key of its own is needed --
    def acting(self) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT account_id, name, updated_at, updated_by FROM acting WHERE id = 1").fetchone()
        return dict(r) if r else None

    def act_for(self, account_id: str, name: str, by: str) -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO acting VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET account_id = excluded.account_id,"
                      " name = excluded.name, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                      (account_id, name, datetime.now(UTC).isoformat(timespec="seconds"), by))

    def stop_acting(self) -> None:
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM acting")

    # -- the account cards are issued on (Airwallex Issuing is enabled per account and per country) --
    def card_account(self) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT account_id, name, updated_at, updated_by FROM card_account WHERE id = 1").fetchone()
        return dict(r) if r else None

    def set_card_account(self, account_id: str | None, name: str, by: str) -> None:
        with self._lock, self._connect() as c:
            if not account_id:
                c.execute("DELETE FROM card_account")
                return
            c.execute("INSERT INTO card_account VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET account_id = excluded.account_id,"
                      " name = excluded.name, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                      (account_id, name, datetime.now(UTC).isoformat(timespec="seconds"), by))


def acting_for() -> str | None:
    """The connected account VENTHORA works for (sent as x-on-behalf-of), or None for the platform's own account."""
    try:
        a = AirwallexStore().acting()
    except sqlite3.Error:
        return None
    return a["account_id"] if a else None


def card_account_for() -> str | None:
    """The connected account cards are issued on (sent as x-on-behalf-of on Issuing calls only), or None."""
    try:
        a = AirwallexStore().card_account()
    except sqlite3.Error:
        return None
    return a["account_id"] if a else None


def credentials() -> tuple[str, str]:
    """(client_id, api_key) VENTHORA authenticates with: the connected account, else .env."""
    try:
        raw = AirwallexStore().raw()
    except sqlite3.Error:
        raw = None
    if raw and raw.get("client_id") and raw.get("api_key"):
        return raw["client_id"], raw["api_key"]
    return env("AIRWALLEX_CLIENT_ID"), env("AIRWALLEX_API_KEY")


def _hint(s: str) -> str | None:
    return f"…{s[-4:]}" if s and len(s) >= 8 else ("set" if s else None)


def public() -> dict:
    """What the dashboard may see: never a secret."""
    raw = AirwallexStore().raw()
    if raw and raw.get("client_id"):
        return {"source": "settings", "client_id_hint": _hint(raw["client_id"]), "key_set": bool(raw.get("api_key")),
                "account_name": raw.get("account_name", ""), "updated_at": raw["_updated_at"], "updated_by": raw["_updated_by"]}
    cid = env("AIRWALLEX_CLIENT_ID")
    return {"source": "env" if cid else "none", "client_id_hint": _hint(cid), "key_set": bool(env("AIRWALLEX_API_KEY")),
            "account_name": "", "updated_at": None, "updated_by": None}
