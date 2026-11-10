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
            c.execute("CREATE TABLE IF NOT EXISTS company_link (id INTEGER PRIMARY KEY CHECK (id = 1), record_json TEXT NOT NULL,"
                      " updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS own_account (account_id TEXT PRIMARY KEY, name TEXT NOT NULL,"
                      " opened_at TEXT NOT NULL, opened_by TEXT NOT NULL)")

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

    # -- the company's own Airwallex account, made before VENTHORA and linked by the FCO with its own API key --
    # It is kept apart from the platform's keys (Developer Console), which stay as they are: unlinking goes back to them.
    def link(self) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT record_json, updated_at, updated_by FROM company_link WHERE id = 1").fetchone()
        return {**json.loads(r["record_json"]), "_updated_at": r["updated_at"], "_updated_by": r["updated_by"]} if r else None

    def save_link(self, rec: dict, by: str) -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO company_link VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET record_json = excluded.record_json,"
                      " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                      (json.dumps(rec), datetime.now(UTC).isoformat(timespec="seconds"), by))

    def clear_link(self) -> None:
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM company_link")

    # -- SaaS: the connected accounts this company opened under the platform (the platform holds every company's) --
    def own_ids(self) -> set[str]:
        with self._connect() as c:
            return {r["account_id"] for r in c.execute("SELECT account_id FROM own_account")}

    def add_own(self, account_id: str, name: str, by: str) -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT OR IGNORE INTO own_account VALUES (?, ?, ?, ?)",
                      (account_id, name, datetime.now(UTC).isoformat(timespec="seconds"), by))

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


def tenant() -> dict | None:
    """SaaS: the company this VENTHORA serves (VENTHORA_TENANT_ID, set by the platform for each company's own process),
    or None for a company running VENTHORA itself."""
    tid = env("VENTHORA_TENANT_ID")
    return {"tenant_id": tid, "name": env("VENTHORA_TENANT_NAME") or tid} if tid else None


def has_own_account() -> bool:
    """Does the company work on an Airwallex account of its own (linked, or opened under the platform and in use)?"""
    return bool(linked() or acting_for())


def owns(account_id: str) -> bool:
    """SaaS: did this company open that connected account? Outside SaaS every account the keys see is the company's."""
    if tenant() is None:
        return True
    try:
        return account_id in AirwallexStore().own_ids()
    except sqlite3.Error:
        return False


def linked() -> dict | None:
    """The company's own account linked by the FCO (its client ID and key), or None."""
    try:
        rec = AirwallexStore().link()
    except sqlite3.Error:
        return None
    return rec if rec and rec.get("client_id") and rec.get("api_key") else None


def acting_for() -> str | None:
    """The connected account VENTHORA works for (sent as x-on-behalf-of), or None for the platform's own account. A
    linked account is called with its own key: nothing is sent on its behalf."""
    if linked():
        return None
    try:
        a = AirwallexStore().acting()
    except sqlite3.Error:
        return None
    return a["account_id"] if a else None


def card_account_for() -> str | None:
    """The connected account cards are issued on (sent as x-on-behalf-of on Issuing calls only), or None."""
    if linked():
        return None  # a linked account issues its cards on itself
    try:
        a = AirwallexStore().card_account()
    except sqlite3.Error:
        return None
    return a["account_id"] if a else None


def credentials() -> tuple[str, str]:
    """(client_id, api_key) VENTHORA authenticates with: the company's linked account, else the platform's keys
    (Developer Console), else .env."""
    lk = linked()
    if lk:
        return lk["client_id"], lk["api_key"]
    try:
        raw = AirwallexStore().raw()
    except sqlite3.Error:
        raw = None
    if raw and raw.get("client_id") and raw.get("api_key"):
        return raw["client_id"], raw["api_key"]
    return env("AIRWALLEX_CLIENT_ID"), env("AIRWALLEX_API_KEY")


def _hint(s: str) -> str | None:
    return f"…{s[-4:]}" if s and len(s) >= 8 else ("set" if s else None)


def public_link() -> dict | None:
    """The linked account as the dashboard may see it: never a secret."""
    rec = linked()
    if rec is None:
        return None
    return {"account_name": rec.get("account_name", ""), "account_id": rec.get("account_id"), "client_id_hint": _hint(rec["client_id"]),
            "key_set": True, "webhook_secret_set": bool(rec.get("webhook_secret")), "linked_at": rec["_updated_at"], "linked_by": rec["_updated_by"]}


def public() -> dict:
    """What the dashboard may see: never a secret. The platform's keys, not a company's linked account."""
    raw = AirwallexStore().raw()
    if raw and raw.get("client_id"):
        return {"source": "settings", "client_id_hint": _hint(raw["client_id"]), "key_set": bool(raw.get("api_key")),
                "account_name": raw.get("account_name", ""), "updated_at": raw["_updated_at"], "updated_by": raw["_updated_by"]}
    cid = env("AIRWALLEX_CLIENT_ID")
    return {"source": "env" if cid else "none", "client_id_hint": _hint(cid), "key_set": bool(env("AIRWALLEX_API_KEY")),
            "account_name": "", "updated_at": None, "updated_by": None}
